"""A WhatsApp photo is confirmed only for the current account and official asset."""
import hashlib
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from profile_photo_status import official_image_matches, whatsapp_profile_picture_status


class WhatsAppProfilePhotoStatusTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.identity = self.root / "identity.json"
        self.state = self.root / "state.json"
        self.official = self.root / "official.png"
        self.official.write_bytes(b"official-test-image")
        self.account = hashlib.sha256(b"570000000001@s.whatsapp.net").hexdigest()
        self.image_hash = hashlib.sha256(self.official.read_bytes()).hexdigest()
        self.identity.write_text(json.dumps({"account_hash": self.account, "phone": "+570000000001"}), encoding="utf-8")

    def record(self, **overrides):
        record = {"status": "applied", "imageHash": self.image_hash,
                  "attemptedAt": time.time() * 1000,
                  "verification": "provider_acknowledged", "raw_error": "private provider detail"}
        record.update(overrides)
        self.state.write_text(json.dumps({"version": 1, "accounts": {self.account: record}}), encoding="utf-8")

    def status(self, image=None, **kwargs):
        return whatsapp_profile_picture_status(self.identity, self.state, image or self.official,
                                               self.official, **kwargs)

    def assert_safe(self, status):
        self.assertLessEqual(set(status), {"status", "message", "verification"})
        encoded = json.dumps(status)
        for private in (self.account, self.image_hash, "+570000000001", "private provider detail"):
            self.assertNotIn(private, encoded)

    def test_matching_account_image_and_provider_acknowledgement_confirm_applied(self):
        self.record()
        status = self.status()
        self.assertEqual("applied", status["status"])
        self.assertEqual("provider_acknowledged", status["verification"])
        self.assert_safe(status)

    def test_applied_record_without_provider_acknowledgement_is_not_confirmation(self):
        for verification in (None, "", "local_hash", "provider_acknowledged_old"):
            with self.subTest(verification=verification):
                self.record(verification=verification)
                status = self.status()
                self.assertEqual("unknown", status["status"])
                self.assert_safe(status)

    def test_acknowledged_photo_for_another_account_is_not_reused(self):
        self.record()
        self.identity.write_text(json.dumps({"account_hash": "b" * 64}), encoding="utf-8")
        self.assertEqual("unknown", self.status()["status"])

    def test_acknowledged_different_image_is_not_reused(self):
        self.record(imageHash="a" * 64)
        self.assertEqual("unknown", self.status()["status"])

    def test_invalid_or_missing_current_identity_does_not_claim_success(self):
        self.record()
        for identity in ({}, {"account_hash": None}, {"account_hash": "malformed"}, []):
            with self.subTest(identity=identity):
                self.identity.write_text(json.dumps(identity), encoding="utf-8")
                self.assertEqual("unknown", self.status()["status"])
        self.identity.unlink()
        self.assertEqual("unknown", self.status()["status"])

    def test_unsupported_or_malformed_state_does_not_claim_success(self):
        for state in ({"version": 2, "accounts": {}}, {"version": 1, "accounts": None}, []):
            with self.subTest(state=state):
                self.state.write_text(json.dumps(state), encoding="utf-8")
                self.assertEqual("unknown", self.status()["status"])
        self.state.write_text("invalid JSON", encoding="utf-8")
        self.assertEqual("unknown", self.status()["status"])

    def test_other_brand_override_blocks_official_success_even_with_matching_state(self):
        self.record()
        other = self.root / "other-brand.png"
        other.write_bytes(b"other-brand")
        self.assertFalse(official_image_matches(other, self.official))
        status = self.status(other)
        self.assertEqual("failed", status["status"])
        self.assert_safe(status)

    def test_official_copy_is_recognized_by_content_not_filename(self):
        self.record()
        copy = self.root / "copy.png"
        copy.write_bytes(self.official.read_bytes())
        self.assertTrue(official_image_matches(copy, self.official))
        self.assertEqual("applied", self.status(copy)["status"])

    def test_missing_asset_and_disabled_policy_cannot_claim_applied(self):
        self.record()
        self.assertEqual("failed", self.status(self.root / "missing.png")["status"])
        self.assertEqual("disabled", self.status(enabled=False)["status"])

    def test_non_success_states_preserve_meaning_without_leaking_raw_records(self):
        for state in ("pending", "uncertain", "failed", "missing"):
            with self.subTest(state=state):
                self.record(status=state)
                public = self.status()
                self.assertEqual(state, public["status"])
                self.assert_safe(public)

    def test_pending_attempt_within_timeout_remains_pending(self):
        with patch("profile_photo_status.time.time", return_value=1000):
            for attempted_at in (1_000_000, 925_000):
                with self.subTest(attempted_at=attempted_at):
                    self.record(status="pending", attemptedAt=attempted_at)
                    self.assertEqual("pending", self.status()["status"])

    def test_stale_future_or_invalid_pending_timestamp_is_uncertain(self):
        with patch("profile_photo_status.time.time", return_value=1000):
            for attempted_at in (924_999, 1_000_001, None, "1000000", {}, float("nan"), float("inf")):
                with self.subTest(attempted_at=attempted_at):
                    self.record(status="pending", attemptedAt=attempted_at)
                    status = self.status()
                    self.assertEqual("uncertain", status["status"])
                    self.assert_safe(status)


if __name__ == "__main__":
    unittest.main()
