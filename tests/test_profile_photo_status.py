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

    def request(self, **overrides):
        now = int(time.time() * 1000)
        request = {"brand": "pecadosvip", "status": "pending", "requestId": "2" * 32,
                   "accountHash": self.account, "imageHash": self.image_hash,
                   "requestedAt": now, "expiresAt": now + 600_000}
        request.update(overrides)
        (self.root / "wa_profile_picture_request.json").write_text(json.dumps(request), encoding="utf-8")
        return request

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
        self.assertIn("última actualización registrada", status["message"])
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

    def test_new_link_request_is_pending_instead_of_reusing_a_historical_result(self):
        self.request()
        for old_status in ("applied", "failed", "missing"):
            for request_id in (None, "1" * 32):
                with self.subTest(old_status=old_status, request_id=request_id):
                    self.record(status=old_status, requestId=request_id)
                    status = self.status()
                    self.assertEqual("pending", status["status"])
                    self.assertNotIn("verification", status)
                    self.assert_safe(status)

    def test_expired_or_invalid_new_request_is_not_hidden_by_an_old_ack(self):
        self.record(requestId="1" * 32)
        now = int(time.time() * 1000)
        for fields in ({"requestedAt": now - 700_000, "expiresAt": now - 100_000},
                       {"brand": "las-fiesteras"}, {"imageHash": "f" * 64}, {"requestId": "invalid"}):
            with self.subTest(fields=fields):
                self.request(**fields)
                status = self.status()
                self.assertEqual("failed", status["status"])
                self.assertNotIn("verification", status)
                self.assert_safe(status)

    def test_current_request_outcome_remains_visible_after_its_authorization_expires(self):
        now = int(time.time() * 1000)
        request = self.request(requestedAt=now - 700_000, expiresAt=now - 100_000)
        for outcome in ("applied", "failed", "uncertain"):
            with self.subTest(outcome=outcome):
                self.record(requestId=request["requestId"], status=outcome)
                self.assertEqual(outcome, self.status()["status"])

    def test_new_request_cannot_hide_an_unresolved_operation_even_for_an_old_image(self):
        self.request()
        for image_hash in (self.image_hash, "f" * 64):
            for old_status in ("pending", "uncertain"):
                with self.subTest(image_hash=image_hash, old_status=old_status):
                    self.record(requestId="1" * 32, imageHash=image_hash, status=old_status,
                                attemptedAt=time.time() * 1000 - 100_000)
                    status = self.status()
                    self.assertEqual("uncertain", status["status"])
                    self.assert_safe(status)

    def test_new_request_preserves_visible_cooldown_then_becomes_pending_after_wait(self):
        with patch("profile_photo_status.time.time", return_value=1000):
            self.request()
            for image_hash in (self.image_hash, "f" * 64):
                with self.subTest(image_hash=image_hash):
                    self.record(requestId="1" * 32, imageHash=image_hash, status="failed",
                                errorCode="rate_limited", retryAfter=1_001_000)
                    status = self.status()
                    self.assertEqual("failed", status["status"])
                    self.assertIn("limitó", status["message"])
                    self.assert_safe(status)
            with patch("profile_photo_status.time.time", return_value=1002):
                self.assertEqual("pending", self.status()["status"])

    def test_request_for_another_account_does_not_replace_current_account_status(self):
        self.record(requestId="1" * 32)
        self.request(accountHash="b" * 64)
        status = self.status()
        self.assertEqual("applied", status["status"])
        self.assert_safe(status)

    def test_unreadable_or_malformed_request_cannot_reuse_historical_success(self):
        self.record(requestId="1" * 32)
        request_path = self.root / "wa_profile_picture_request.json"
        for contents in ("invalid JSON", "[]", "null", "{}", '{"accountHash": "invalid"}'):
            with self.subTest(contents=contents):
                request_path.write_text(contents, encoding="utf-8")
                status = self.status()
                self.assertEqual("failed", status["status"])
                self.assert_safe(status)
        original_read = Path.read_text

        def read(path, *args, **kwargs):
            if path == request_path:
                raise PermissionError("private filesystem detail")
            return original_read(path, *args, **kwargs)

        with patch.object(Path, "read_text", read):
            self.assertEqual("failed", self.status()["status"])
        request_path.unlink()
        self.assertEqual("applied", self.status()["status"])


if __name__ == "__main__":
    unittest.main()
