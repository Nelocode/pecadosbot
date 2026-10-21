"""Offline tests for durable video holds; no SDK, app or provider is imported."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from telegram_delivery_journal import (
    TelegramVideoDeliveryJournal,
    TelegramVideoDeliveryUncertain,
)


class TelegramDeliveryJournalTests(unittest.TestCase):
    ACCOUNT = "+570000000001"
    OTHER_ACCOUNT = "+570000000002"
    FINGERPRINT = "a" * 64

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name) / "journal"

    def journal(self, account=None, **kwargs):
        return TelegramVideoDeliveryJournal(self.directory, account or self.ACCOUNT, **kwargs)

    def test_constructor_has_no_filesystem_effect_and_normalizes_account(self):
        journal = self.journal()
        self.assertFalse(self.directory.exists())
        normalized = self.journal("+57 (000) 000-0001")
        self.assertEqual(journal.account_hash, normalized.account_hash)
        self.assertEqual("telegram_video_delivery.sqlite3", journal.path.name)

    def test_invalid_account_limit_and_fingerprint_fail_before_filesystem_access(self):
        for account in (None, "", "123", "57secret0000000001", "1" * 16):
            with self.subTest(account=account), self.assertRaises(TelegramVideoDeliveryUncertain):
                TelegramVideoDeliveryJournal(self.directory, account)
        for limit in (0, -1, True, 1.5):
            with self.subTest(limit=limit), self.assertRaises(TelegramVideoDeliveryUncertain):
                self.journal(max_records=limit)
        for fingerprint in (None, "", "a" * 63, "G" * 64, "../" + "a" * 61):
            for method in ("check", "claim", "confirmed", "rejected", "completed"):
                with self.subTest(method=method, fingerprint=fingerprint):
                    with self.assertRaises(TelegramVideoDeliveryUncertain):
                        getattr(self.journal(), method)(fingerprint)
        self.assertFalse(self.directory.exists())

    def test_directory_and_sqlite_artifact_links_are_blocked_before_connect(self):
        journal = self.journal()
        targets = (self.directory, journal.path, *(Path(str(journal.path) + suffix) for suffix in ("-journal", "-wal", "-shm")))
        original_is_symlink = Path.is_symlink
        for target in targets:
            with self.subTest(target=target.name):
                with (
                    patch.object(Path, "is_symlink", lambda path: path == target or original_is_symlink(path)),
                    patch("telegram_delivery_journal.sqlite3.connect") as connect,
                ):
                    with self.assertRaisesRegex(TelegramVideoDeliveryUncertain, "unsafe_path"):
                        journal.claim(self.FINGERPRINT)
                    connect.assert_not_called()
        self.assertFalse(self.directory.exists())

    def test_junction_and_resolved_escape_are_blocked_before_connect(self):
        journal = self.journal()
        original_resolve = Path.resolve
        outside = self.directory.parent / "outside"
        for target in (self.directory.parent, self.directory, journal.path):
            with self.subTest(target=target.name):
                def redirected(path, *args, **kwargs):
                    return outside if path == target else original_resolve(path, *args, **kwargs)
                with (
                    patch.object(Path, "resolve", redirected),
                    patch("telegram_delivery_journal.sqlite3.connect") as connect,
                ):
                    with self.assertRaisesRegex(TelegramVideoDeliveryUncertain, "unsafe_path"):
                        journal.check(self.FINGERPRINT)
                    connect.assert_not_called()
        with (
            patch.object(Path, "is_junction", lambda path: path == self.directory, create=True),
            patch("telegram_delivery_journal.sqlite3.connect") as connect,
        ):
            with self.assertRaisesRegex(TelegramVideoDeliveryUncertain, "unsafe_path"):
                journal.check(self.FINGERPRINT)
            connect.assert_not_called()
        self.assertFalse(self.directory.exists())

    def test_real_sidecar_symlink_cannot_modify_its_external_target(self):
        journal = self.journal()
        self.directory.mkdir()
        outside = self.directory.parent / "outside-evidence"
        outside.write_bytes(b"external fixture evidence")
        link = Path(str(journal.path) + "-journal")
        try:
            link.symlink_to(outside)
        except (OSError, NotImplementedError):
            self.skipTest("Local platform does not permit test symlinks; mocked path guards are covered")
        with self.assertRaises(TelegramVideoDeliveryUncertain):
            journal.claim(self.FINGERPRINT)
        self.assertEqual(b"external fixture evidence", outside.read_bytes())
        self.assertFalse(journal.path.exists())

    def test_pending_survives_reload_and_blocks_check_and_claim(self):
        self.assertFalse(self.journal().check(self.FINGERPRINT))
        self.assertTrue(self.journal().claim(self.FINGERPRINT))
        for method in ("check", "claim"):
            with self.assertRaisesRegex(TelegramVideoDeliveryUncertain, "delivery_uncertain"):
                getattr(self.journal(), method)(self.FINGERPRINT)

    def test_ack_survives_reload_and_duplicate_claim_cannot_resend(self):
        self.journal().claim(self.FINGERPRINT)
        self.journal().confirmed(self.FINGERPRINT)
        self.assertTrue(self.journal().check(self.FINGERPRINT))
        self.assertFalse(self.journal().claim(self.FINGERPRINT))
        self.journal().confirmed(self.FINGERPRINT)
        with self.assertRaises(TelegramVideoDeliveryUncertain):
            self.journal().rejected(self.FINGERPRINT)
        self.assertTrue(self.journal().check(self.FINGERPRINT))

    def test_accounts_are_isolated_and_database_never_stores_phone(self):
        first = self.journal()
        first.claim(self.FINGERPRINT)
        other = self.journal(self.OTHER_ACCOUNT)
        self.assertFalse(other.check(self.FINGERPRINT))
        self.assertTrue(other.claim(self.FINGERPRINT))
        other.confirmed(self.FINGERPRINT)
        with self.assertRaises(TelegramVideoDeliveryUncertain):
            first.check(self.FINGERPRINT)
        for file_path in self.directory.iterdir():
            data = file_path.read_bytes()
            self.assertNotIn(self.ACCOUNT.lstrip("+").encode(), data)
            self.assertNotIn(self.OTHER_ACCOUNT.lstrip("+").encode(), data)

    def test_rejection_releases_only_a_previously_pending_attempt(self):
        journal = self.journal()
        for method in ("confirmed", "rejected"):
            with self.assertRaises(TelegramVideoDeliveryUncertain):
                getattr(journal, method)(self.FINGERPRINT)
        journal.claim(self.FINGERPRINT)
        journal.rejected(self.FINGERPRINT)
        self.assertFalse(self.journal().check(self.FINGERPRINT))
        self.assertTrue(self.journal().claim(self.FINGERPRINT))

    def test_capacity_is_global_and_does_not_prune_pending_or_sent(self):
        journal = self.journal(max_records=2)
        journal.claim(self.FINGERPRINT)
        journal.claim("b" * 64)
        journal.confirmed("b" * 64)
        with self.assertRaisesRegex(TelegramVideoDeliveryUncertain, "journal_full"):
            self.journal(self.OTHER_ACCOUNT, max_records=2).claim("c" * 64)
        with self.assertRaises(TelegramVideoDeliveryUncertain):
            self.journal().check(self.FINGERPRINT)
        self.assertTrue(self.journal().check("b" * 64))

    def test_corrupt_database_fails_closed_without_replacing_evidence(self):
        self.directory.mkdir()
        path = self.journal().path
        data = b"corrupt previous journal evidence"
        path.write_bytes(data)
        for method in ("check", "claim", "confirmed", "rejected", "completed"):
            with self.assertRaises(TelegramVideoDeliveryUncertain):
                getattr(self.journal(), method)(self.FINGERPRINT)
        self.assertEqual(data, path.read_bytes())

    def test_unknown_schema_and_missing_table_fail_closed(self):
        journal = self.journal()
        journal.check(self.FINGERPRINT)
        with closing(sqlite3.connect(journal.path)) as connection:
            connection.execute("PRAGMA user_version = 2")
        with self.assertRaises(TelegramVideoDeliveryUncertain):
            journal.claim(self.FINGERPRINT)
        with closing(sqlite3.connect(journal.path)) as connection:
            connection.execute("PRAGMA user_version = 1")
            connection.execute("DROP TABLE deliveries")
        with self.assertRaises(TelegramVideoDeliveryUncertain):
            journal.check(self.FINGERPRINT)

    def test_unreadable_storage_raises_sanitized_typed_error(self):
        with patch("telegram_delivery_journal.sqlite3.connect", side_effect=sqlite3.OperationalError("private path 570000000001")):
            with self.assertRaises(TelegramVideoDeliveryUncertain) as caught:
                self.journal().claim(self.FINGERPRINT)
        self.assertEqual("telegram_video_journal_unavailable", str(caught.exception))

    def test_failed_prewrite_never_reports_claim_success(self):
        journal = self.journal()
        journal.check(self.FINGERPRINT)
        with patch.object(journal, "_commit", side_effect=sqlite3.OperationalError("disk full")):
            with self.assertRaises(TelegramVideoDeliveryUncertain):
                journal.claim(self.FINGERPRINT)
        self.assertFalse(self.journal().check(self.FINGERPRINT))

    def test_failed_ack_write_preserves_pending_after_reload(self):
        journal = self.journal()
        journal.claim(self.FINGERPRINT)
        with patch.object(journal, "_commit", side_effect=sqlite3.OperationalError("disk full")):
            with self.assertRaises(TelegramVideoDeliveryUncertain):
                journal.confirmed(self.FINGERPRINT)
        with self.assertRaises(TelegramVideoDeliveryUncertain):
            self.journal().check(self.FINGERPRINT)
        with self.assertRaises(TelegramVideoDeliveryUncertain):
            self.journal().claim(self.FINGERPRINT)

    def test_failed_rejection_write_cannot_release_pending(self):
        journal = self.journal()
        journal.claim(self.FINGERPRINT)
        with patch.object(journal, "_commit", side_effect=sqlite3.OperationalError("disk full")):
            with self.assertRaises(TelegramVideoDeliveryUncertain):
                journal.rejected(self.FINGERPRINT)
        with self.assertRaises(TelegramVideoDeliveryUncertain):
            self.journal().claim(self.FINGERPRINT)

    def test_completed_releases_only_confirmed_video_and_frees_capacity(self):
        journal = self.journal(max_records=1)
        journal.claim(self.FINGERPRINT)
        journal.confirmed(self.FINGERPRINT)
        journal.completed(self.FINGERPRINT)
        self.assertFalse(self.journal().check(self.FINGERPRINT))
        journal.completed(self.FINGERPRINT)
        self.assertTrue(journal.claim("b" * 64))

    def test_completed_never_releases_pending_or_another_account_record(self):
        journal = self.journal()
        journal.claim(self.FINGERPRINT)
        with self.assertRaises(TelegramVideoDeliveryUncertain):
            journal.completed(self.FINGERPRINT)
        self.journal(self.OTHER_ACCOUNT).completed(self.FINGERPRINT)
        with self.assertRaises(TelegramVideoDeliveryUncertain):
            self.journal().claim(self.FINGERPRINT)

    def test_failed_completed_write_retains_confirmed_evidence(self):
        journal = self.journal()
        journal.claim(self.FINGERPRINT)
        journal.confirmed(self.FINGERPRINT)
        with patch.object(journal, "_commit", side_effect=sqlite3.OperationalError("disk full")):
            with self.assertRaises(TelegramVideoDeliveryUncertain):
                journal.completed(self.FINGERPRINT)
        self.assertTrue(self.journal().check(self.FINGERPRINT))
        self.assertFalse(self.journal().claim(self.FINGERPRINT))

    def test_concurrent_claims_allow_exactly_one_new_attempt(self):
        barrier = threading.Barrier(2)

        def claim():
            barrier.wait(timeout=5)
            try:
                return self.journal().claim(self.FINGERPRINT)
            except TelegramVideoDeliveryUncertain:
                return "blocked"

        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(executor.map(lambda _: claim(), range(2)))
        self.assertCountEqual([True, "blocked"], outcomes)


if __name__ == "__main__":
    unittest.main()
