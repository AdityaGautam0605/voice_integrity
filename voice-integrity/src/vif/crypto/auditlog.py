"""Tamper-evident audit log.

Escalating rigour, and the level matters:

    1. hash chain          detects any edit or deletion
    2. Merkle tree         efficient proof that one entry is present
    3. signed checkpoints  makes the log non-repudiable, not merely
                           self-consistent

Level 1 alone protects against nothing if the attacker can recompute the
chain, which is why checkpoints are signed here rather than left as a
"future work" note.

What the log contains: verdicts.  What it never contains: audio, features,
embeddings or transcript (FR-VE-05).  An auditor can prove which decisions
were made without gaining access to a single second of anyone's speech.
"""

from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from vif.common.logging import get_logger
from vif.common.types import Verdict
from vif.crypto.verdict import SigningKeyPair

log = get_logger(__name__)

GENESIS_HASH = "0" * 64

_SCHEMA = """
CREATE TABLE IF NOT EXISTS entries (
    seq         INTEGER PRIMARY KEY,
    prev_hash   TEXT NOT NULL,
    entry_hash  TEXT NOT NULL,
    payload     TEXT NOT NULL,
    ts_ms       INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS checkpoints (
    seq         INTEGER PRIMARY KEY,
    merkle_root TEXT NOT NULL,
    signature   TEXT NOT NULL,
    key_id      TEXT NOT NULL,
    ts_ms       INTEGER NOT NULL
);
"""

_FORBIDDEN = ("audio", "pcm", "waveform", "embedding", "features", "transcript")


@dataclass
class LogEntry:
    seq: int
    prev_hash: str
    entry_hash: str
    payload: dict
    ts_ms: int


def _hash_entry(seq: int, prev_hash: str, payload_json: str) -> str:
    material = f"{seq}|{prev_hash}|{payload_json}".encode()
    return hashlib.sha256(material).hexdigest()


def merkle_root(hashes: list[str]) -> str:
    """Root over the entry hashes.

    Odd nodes are promoted rather than duplicated, which avoids the
    duplicate-leaf ambiguity that bit Bitcoin's implementation.
    """
    if not hashes:
        return GENESIS_HASH
    level = list(hashes)
    while len(level) > 1:
        nxt = []
        for i in range(0, len(level) - 1, 2):
            nxt.append(hashlib.sha256((level[i] + level[i + 1]).encode()).hexdigest())
        if len(level) % 2:
            nxt.append(level[-1])
        level = nxt
    return level[0]


class AuditLog:
    """Append-only, hash-chained store of signed verdicts."""

    def __init__(self, db_path: str | Path, signer: SigningKeyPair | None = None):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.signer = signer
        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # -- append ------------------------------------------------------------

    def append(self, verdict: Verdict) -> LogEntry:
        """Add a verdict.  Refuses anything carrying personal content."""
        payload = verdict.model_dump(mode="json")
        self._reject_personal_data(payload)

        payload_json = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        prev_hash, seq = self._tip()
        entry_hash = _hash_entry(seq, prev_hash, payload_json)
        ts_ms = int(time.time() * 1000)

        self._conn.execute(
            "INSERT INTO entries VALUES (?,?,?,?,?)",
            (seq, prev_hash, entry_hash, payload_json, ts_ms),
        )
        self._conn.commit()
        return LogEntry(
            seq=seq, prev_hash=prev_hash, entry_hash=entry_hash, payload=payload, ts_ms=ts_ms
        )

    def _tip(self) -> tuple[str, int]:
        row = self._conn.execute(
            "SELECT entry_hash, seq FROM entries ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        return (GENESIS_HASH, 0) if row is None else (row[0], row[1] + 1)

    @staticmethod
    def _reject_personal_data(payload: dict) -> None:
        blob = json.dumps(payload).lower()
        for token in _FORBIDDEN:
            if f'"{token}"' in blob:
                raise ValueError(
                    f"refusing to log a payload containing '{token}'. "
                    "The audit log holds decisions, never content."
                )

    # -- verification ------------------------------------------------------

    def verify_chain(self) -> tuple[bool, str]:
        """Recompute the chain end to end.

        Any edit, deletion or reordering breaks it.  Note the limit: an
        attacker who can rewrite the whole table can also recompute every
        hash - which is exactly why checkpoints are signed and, in production,
        anchored externally (SEC-14).
        """
        rows = self._conn.execute(
            "SELECT seq, prev_hash, entry_hash, payload FROM entries ORDER BY seq"
        ).fetchall()
        expected_prev = GENESIS_HASH
        for seq, prev_hash, entry_hash, payload_json in rows:
            if prev_hash != expected_prev:
                return False, f"entry {seq}: prev_hash does not match the previous entry"
            recomputed = _hash_entry(seq, prev_hash, payload_json)
            if recomputed != entry_hash:
                return False, f"entry {seq}: payload was altered after it was written"
            expected_prev = entry_hash
        return True, f"chain intact across {len(rows)} entries"

    # -- checkpoints -------------------------------------------------------

    def checkpoint(self) -> dict | None:
        """Sign a Merkle root over everything written so far."""
        if self.signer is None:
            log.warning("no signing key configured - checkpoint skipped")
            return None

        rows = self._conn.execute("SELECT entry_hash FROM entries ORDER BY seq").fetchall()
        if not rows:
            return None

        root = merkle_root([r[0] for r in rows])
        signature = self.signer.private_key.sign(root.encode())
        seq = len(rows) - 1
        ts_ms = int(time.time() * 1000)

        self._conn.execute(
            "INSERT OR REPLACE INTO checkpoints VALUES (?,?,?,?,?)",
            (seq, root, base64.b64encode(signature).decode(), self.signer.key_id, ts_ms),
        )
        self._conn.commit()
        log.info("checkpointed %d entries, root %s", len(rows), root[:16])
        return {
            "seq": seq,
            "merkle_root": root,
            "signature": base64.b64encode(signature).decode(),
            "key_id": self.signer.key_id,
            "ts_ms": ts_ms,
        }

    def verify_checkpoint(self, public_key) -> tuple[bool, str]:
        row = self._conn.execute(
            "SELECT seq, merkle_root, signature FROM checkpoints ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return False, "no checkpoint recorded"

        seq, root, signature = row
        rows = self._conn.execute(
            "SELECT entry_hash FROM entries WHERE seq <= ? ORDER BY seq", (seq,)
        ).fetchall()
        if merkle_root([r[0] for r in rows]) != root:
            return False, "entries no longer produce the checkpointed root"

        try:
            public_key.verify(base64.b64decode(signature), root.encode())
        except Exception as exc:  # noqa: BLE001
            return False, f"checkpoint signature invalid: {exc}"
        return True, f"checkpoint at seq {seq} verifies"

    # -- read --------------------------------------------------------------

    def entries(self, limit: int = 100) -> list[LogEntry]:
        rows = self._conn.execute(
            "SELECT seq, prev_hash, entry_hash, payload, ts_ms FROM entries "
            "ORDER BY seq DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [
            LogEntry(
                seq=r[0], prev_hash=r[1], entry_hash=r[2], payload=json.loads(r[3]), ts_ms=r[4]
            )
            for r in rows
        ]

    def count(self) -> int:
        return int(self._conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0])
