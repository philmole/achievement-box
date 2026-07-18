"""Authenticated, encrypted persistence for offline RA submissions."""

from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken


DEFAULT_QUEUE_DIR = Path(__file__).parent.parent / "cache" / "ra-offline"


@dataclass(frozen=True)
class QueuedSubmission:
    id: str
    url: str
    post_data: bytes | None
    content_type: str


class OfflineSubmissionQueue:
    """A deduplicated SQLite index containing only Fernet ciphertext.

    The local key and database are deliberately separate files so a copied
    database does not disclose the RA token embedded in submission payloads.
    Fernet also authenticates each record; damaged or modified records are
    rejected instead of being sent.
    """

    def __init__(self, directory: Path = DEFAULT_QUEUE_DIR):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.key_path = self.directory / "queue.key"
        self.db_path = self.directory / "submissions.sqlite3"
        self._lock = threading.RLock()
        self._fernet = Fernet(self._load_or_create_key())
        with self._database() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS submissions ("
                "id TEXT PRIMARY KEY, payload BLOB NOT NULL, "
                "created_at INTEGER NOT NULL)"
            )
        self._restrict_permissions(self.db_path)

    @staticmethod
    def _restrict_permissions(path: Path) -> None:
        try:
            path.chmod(0o600)
        except OSError:
            pass

    def _load_or_create_key(self) -> bytes:
        try:
            key = self.key_path.read_bytes().strip()
        except FileNotFoundError:
            key = Fernet.generate_key()
            try:
                with self.key_path.open("xb") as key_file:
                    key_file.write(key + b"\n")
            except FileExistsError:  # another startup won the race
                key = self.key_path.read_bytes().strip()
        self._restrict_permissions(self.key_path)
        return key

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path, timeout=10)

    @contextmanager
    def _database(self):
        db = self._connect()
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _id_for(url: str, post_data: bytes | None, content_type: str) -> str:
        digest = hashlib.sha256()
        for value in (url.encode("utf-8"), post_data or b"",
                      content_type.encode("utf-8")):
            digest.update(len(value).to_bytes(8, "big"))
            digest.update(value)
        return digest.hexdigest()

    def enqueue(self, url: str, post_data: bytes | None,
                content_type: str) -> str:
        item_id = self._id_for(url, post_data, content_type)
        plain = json.dumps({
            "url": url,
            "post_data": (base64.b64encode(post_data).decode("ascii")
                          if post_data is not None else None),
            "content_type": content_type,
        }, separators=(",", ":")).encode("utf-8")
        ciphertext = self._fernet.encrypt(plain)
        with self._lock, self._database() as db:
            db.execute(
                "INSERT OR IGNORE INTO submissions(id, payload, created_at) "
                "VALUES (?, ?, ?)", (item_id, ciphertext, int(time.time())))
        return item_id

    def pending(self, only_ids: set[str] | None = None,
                on_corrupt=None) -> list[QueuedSubmission]:
        with self._lock, self._database() as db:
            rows = db.execute(
                "SELECT id, payload FROM submissions ORDER BY created_at, id"
            ).fetchall()
            output = []
            corrupt = []
            for item_id, ciphertext in rows:
                if only_ids is not None and item_id not in only_ids:
                    continue
                try:
                    decoded = json.loads(
                        self._fernet.decrypt(ciphertext).decode("utf-8"))
                    post_data = decoded["post_data"]
                    output.append(QueuedSubmission(
                        id=item_id,
                        url=decoded["url"],
                        post_data=(base64.b64decode(post_data, validate=True)
                                   if post_data is not None else None),
                        content_type=decoded["content_type"],
                    ))
                except (InvalidToken, UnicodeDecodeError, json.JSONDecodeError,
                        KeyError, TypeError, ValueError):
                    corrupt.append(item_id)
            if corrupt:
                db.executemany("DELETE FROM submissions WHERE id = ?",
                               ((item_id,) for item_id in corrupt))
                if on_corrupt is not None:
                    for item_id in corrupt:
                        on_corrupt(item_id)
            return output

    def ids(self) -> set[str]:
        with self._lock, self._database() as db:
            return {row[0] for row in db.execute(
                "SELECT id FROM submissions").fetchall()}

    def remove(self, item_id: str) -> None:
        with self._lock, self._database() as db:
            db.execute("DELETE FROM submissions WHERE id = ?", (item_id,))

    def __len__(self) -> int:
        with self._lock, self._database() as db:
            return db.execute(
                "SELECT COUNT(*) FROM submissions").fetchone()[0]
