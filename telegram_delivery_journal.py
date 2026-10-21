"""Durable, account-scoped holds for Telegram video delivery attempts."""

from contextlib import contextmanager
import hashlib
from pathlib import Path
import re
import sqlite3


class TelegramVideoDeliveryUncertain(RuntimeError):
    """Video delivery cannot safely continue or be attempted again."""

    component = "video"


class TelegramVideoDeliveryJournal:
    """Retain pending/sent records without expiring unresolved delivery evidence.

    Construction performs no filesystem access. Call ``claim`` immediately
    before the provider RPC, ``confirmed`` only after its acknowledgement, and
    ``rejected`` only after an unequivocal rejection. A pending record found by
    a later attempt remains blocked, including after a process restart.
    """

    def __init__(self, directory, account_phone, max_records=10_000):
        if not isinstance(account_phone, str) or not re.fullmatch(
            r"\+?[0-9()\s-]+", account_phone.strip()
        ):
            raise TelegramVideoDeliveryUncertain("telegram_video_journal_invalid_account")
        digits = re.sub(r"\D", "", account_phone)
        if not 7 <= len(digits) <= 15:
            raise TelegramVideoDeliveryUncertain("telegram_video_journal_invalid_account")
        if isinstance(max_records, bool) or not isinstance(max_records, int) or max_records < 1:
            raise TelegramVideoDeliveryUncertain("telegram_video_journal_invalid_limit")
        try:
            directory = Path(directory).absolute()
        except (TypeError, ValueError):
            raise TelegramVideoDeliveryUncertain("telegram_video_journal_invalid_directory") from None
        if ".." in directory.parts:
            raise TelegramVideoDeliveryUncertain("telegram_video_journal_invalid_directory")
        self.path = directory / "telegram_video_delivery.sqlite3"
        self.account_hash = hashlib.sha256(digits.encode("ascii")).hexdigest()
        self.max_records = max_records

    @staticmethod
    def _fingerprint(value):
        if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
            raise TelegramVideoDeliveryUncertain("telegram_video_journal_invalid_fingerprint")
        return value

    @staticmethod
    def _commit(connection):
        connection.commit()

    def _validate_paths(self):
        """Keep every SQLite artifact under the caller's resolved DATA_DIR."""
        directory = self.path.parent
        data_directory = directory.parent
        artifacts = (directory, self.path, *(Path(str(self.path) + suffix) for suffix in ("-journal", "-wal", "-shm")))
        try:
            if data_directory.resolve() != data_directory:
                raise TelegramVideoDeliveryUncertain("telegram_video_journal_unsafe_path")
            for target in artifacts:
                if target.is_symlink() or getattr(target, "is_junction", lambda: False)():
                    raise TelegramVideoDeliveryUncertain("telegram_video_journal_unsafe_path")
                expected_parent = data_directory if target == directory else directory
                resolved = target.resolve()
                if resolved != target or resolved.parent != expected_parent:
                    raise TelegramVideoDeliveryUncertain("telegram_video_journal_unsafe_path")
        except (OSError, RuntimeError) as exc:
            if isinstance(exc, TelegramVideoDeliveryUncertain):
                raise
            raise TelegramVideoDeliveryUncertain("telegram_video_journal_unsafe_path") from None

    @contextmanager
    def _transaction(self):
        connection = None
        try:
            self._validate_paths()
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._validate_paths()
            connection = sqlite3.connect(self.path, timeout=5, isolation_level=None)
            connection.execute("PRAGMA synchronous = FULL")
            connection.execute("BEGIN IMMEDIATE")
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version == 0:
                tables = connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
                ).fetchall()
                if tables:
                    raise TelegramVideoDeliveryUncertain("telegram_video_journal_invalid_schema")
                connection.execute(
                    "CREATE TABLE deliveries ("
                    "account_hash TEXT NOT NULL, fingerprint TEXT NOT NULL, "
                    "status TEXT NOT NULL CHECK(status IN ('pending', 'sent')), "
                    "PRIMARY KEY(account_hash, fingerprint)) WITHOUT ROWID"
                )
                connection.execute("PRAGMA user_version = 1")
            elif version != 1:
                raise TelegramVideoDeliveryUncertain("telegram_video_journal_invalid_schema")
            yield connection
            self._commit(connection)
        except (sqlite3.Error, OSError):
            raise TelegramVideoDeliveryUncertain("telegram_video_journal_unavailable") from None
        finally:
            if connection is not None:
                try:
                    try:
                        if connection.in_transaction:
                            connection.rollback()
                    finally:
                        connection.close()
                except sqlite3.Error:
                    raise TelegramVideoDeliveryUncertain("telegram_video_journal_unavailable") from None

    def _status(self, connection, fingerprint):
        row = connection.execute(
            "SELECT status FROM deliveries WHERE account_hash = ? AND fingerprint = ?",
            (self.account_hash, fingerprint),
        ).fetchone()
        if row is None:
            return None
        if row[0] not in ("pending", "sent"):
            raise TelegramVideoDeliveryUncertain("telegram_video_journal_invalid_record")
        return row[0]

    def check(self, fingerprint):
        """Return True for acknowledged video, False if no attempt was recorded."""
        fingerprint = self._fingerprint(fingerprint)
        with self._transaction() as connection:
            status = self._status(connection, fingerprint)
            if status == "pending":
                raise TelegramVideoDeliveryUncertain("telegram_video_delivery_uncertain")
            return status == "sent"

    def claim(self, fingerprint):
        """Durably reserve a new RPC; existing pending attempts never retry."""
        fingerprint = self._fingerprint(fingerprint)
        with self._transaction() as connection:
            status = self._status(connection, fingerprint)
            if status == "sent":
                return False
            if status == "pending":
                raise TelegramVideoDeliveryUncertain("telegram_video_delivery_uncertain")
            count = connection.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0]
            if count >= self.max_records:
                raise TelegramVideoDeliveryUncertain("telegram_video_journal_full")
            connection.execute(
                "INSERT INTO deliveries(account_hash, fingerprint, status) VALUES (?, ?, 'pending')",
                (self.account_hash, fingerprint),
            )
            return True

    def confirmed(self, fingerprint):
        """Persist an ACK before allowing later response components to proceed."""
        fingerprint = self._fingerprint(fingerprint)
        with self._transaction() as connection:
            status = self._status(connection, fingerprint)
            if status is None:
                raise TelegramVideoDeliveryUncertain("telegram_video_journal_missing_claim")
            if status == "pending":
                connection.execute(
                    "UPDATE deliveries SET status = 'sent' WHERE account_hash = ? AND fingerprint = ?",
                    (self.account_hash, fingerprint),
                )

    def rejected(self, fingerprint):
        """Release only a pending attempt with a caller-confirmed RPC rejection."""
        fingerprint = self._fingerprint(fingerprint)
        with self._transaction() as connection:
            if self._status(connection, fingerprint) != "pending":
                raise TelegramVideoDeliveryUncertain("telegram_video_journal_not_pending")
            connection.execute(
                "DELETE FROM deliveries WHERE account_hash = ? AND fingerprint = ?",
                (self.account_hash, fingerprint),
            )

    def completed(self, fingerprint):
        """Release sent evidence only after the caller durably commits the event.

        A missing record is an idempotent no-op. Pending evidence must never be
        pruned, including when an unrelated interaction was committed.
        """
        fingerprint = self._fingerprint(fingerprint)
        with self._transaction() as connection:
            status = self._status(connection, fingerprint)
            if status == "pending":
                raise TelegramVideoDeliveryUncertain("telegram_video_delivery_uncertain")
            if status == "sent":
                connection.execute(
                    "DELETE FROM deliveries WHERE account_hash = ? AND fingerprint = ?",
                    (self.account_hash, fingerprint),
                )
