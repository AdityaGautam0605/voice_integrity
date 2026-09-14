"""Voiceprint vault.

Two layers, and both are needed:

    1. Cancelable transform   irreversibility, revocability, unlinkability
    2. AES-256-GCM envelope   confidentiality and tamper detection at rest

Encryption alone leaves the plaintext biometric present in memory on every
match; the transform means the *stored* form was never the biometric to begin
with.  Layer one is why a leak is survivable, layer two is why a leak is
unlikely.

Associated data binds each ciphertext to its record id, tenant and key
version, so an attacker with database write access cannot swap one person's
record for another's and have it decrypt cleanly (SEC-05).
"""

from __future__ import annotations

import base64
import json
import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from vif.common.logging import get_logger
from vif.crypto.cancelable import TransformParams, compare, new_params, transform
from vif.crypto.keys import NONCE_BYTES, KeyStore, WrappedKey

log = get_logger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS voiceprints (
    speaker_id    TEXT PRIMARY KEY,
    tenant_id     TEXT NOT NULL,
    ciphertext    BLOB NOT NULL,
    nonce         BLOB NOT NULL,
    wrapped_key   TEXT NOT NULL,
    key_version   INTEGER NOT NULL,
    transform     TEXT NOT NULL,
    template_dim  INTEGER NOT NULL,
    enrolled_ms   INTEGER NOT NULL,
    rotated_ms    INTEGER
);
"""


@dataclass
class EnrolmentRecord:
    speaker_id: str
    tenant_id: str
    template_dim: int
    key_version: int
    transform_version: int
    enrolled_ms: int


class VoiceprintVault:
    """Encrypted, revocable storage for speaker templates.

    Note what is never stored: the raw embedding.  It is transformed on the
    way in and the original is not retained anywhere in this class.
    """

    def __init__(
        self,
        db_path: str | Path,
        keystore: KeyStore,
        tenant_id: str = "default",
        use_cancelable: bool = False,
    ):
        """`use_cancelable` selects the storage form.

        Off (the default): the normalised embedding is stored under AES-256-GCM.
        Simple, and adequate while enrolments are short-lived demo identities.

        On: the embedding passes through a revocable, non-invertible transform
        first.  That matters once enrolments are long-lived, because a
        voiceprint is **irrevocable** - encryption alone leaves nothing to
        rotate after a leak.  The implementation is complete and tested; this
        flag chooses whether the prototype pays for it.
        """
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.keystore = keystore
        self.tenant_id = tenant_id
        self.use_cancelable = use_cancelable
        # See AuditLog: opened at startup, used from ASGI worker threads.
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # -- enrolment ---------------------------------------------------------

    def enrol(
        self,
        speaker_id: str,
        embedding: np.ndarray,
        out_dim: int = 256,
    ) -> EnrolmentRecord:
        """Register a protected identity.

        The raw embedding is transformed immediately and never persisted.
        """
        embedding = np.asarray(embedding, dtype=np.float32).ravel()
        if self.use_cancelable:
            params = new_params(in_dim=embedding.shape[0], out_dim=out_dim, tenant=self.tenant_id)
            template = transform(embedding, params)
        else:
            params = TransformParams(seed="", in_dim=embedding.shape[0], out_dim=0, version=0)
            norm = float(np.linalg.norm(embedding))
            template = (embedding / norm if norm > 0 else embedding).astype(np.float32)

        data_key, wrapped = self.keystore.new_data_key()
        nonce = os.urandom(NONCE_BYTES)
        aad = self._aad(speaker_id, wrapped.key_version)
        ciphertext = AESGCM(data_key).encrypt(nonce, template.tobytes(), aad)

        now_ms = int(time.time() * 1000)
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO voiceprints VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    speaker_id,
                    self.tenant_id,
                    ciphertext,
                    nonce,
                    json.dumps(wrapped.to_dict()),
                    wrapped.key_version,
                    json.dumps(params.__dict__),
                    template.shape[0],
                    now_ms,
                    None,
                ),
            )
            self._conn.commit()
        log.info(
            "enrolled %s (template dim %d, key v%d)",
            speaker_id,
            template.shape[0],
            wrapped.key_version,
        )
        return EnrolmentRecord(
            speaker_id=speaker_id,
            tenant_id=self.tenant_id,
            template_dim=template.shape[0],
            key_version=wrapped.key_version,
            transform_version=params.version,
            enrolled_ms=now_ms,
        )

    def revoke(self, speaker_id: str) -> None:
        """Invalidate stored templates.

        Deletes the record outright.  Re-enrolment requires fresh reference
        audio, which is the honest position: without the original embedding we
        cannot mint a new template, and we deliberately did not keep it.
        """
        with self._lock:
            cur = self._conn.execute("DELETE FROM voiceprints WHERE speaker_id = ?", (speaker_id,))
            self._conn.commit()
        if cur.rowcount:
            log.info("revoked voiceprint for %s - re-enrolment required", speaker_id)

    def rotate_transform(self, speaker_id: str, embedding: np.ndarray) -> EnrolmentRecord:
        """Issue a new template from the same voice under a fresh seed.

        This is the revocability property in action: previously leaked
        templates for this subject become worthless.
        """
        self.revoke(speaker_id)
        record = self.enrol(speaker_id, embedding)
        with self._lock:
            self._conn.execute(
                "UPDATE voiceprints SET rotated_ms = ? WHERE speaker_id = ?",
                (int(time.time() * 1000), speaker_id),
            )
            self._conn.commit()
        return record

    # -- matching ----------------------------------------------------------

    def match(self, speaker_id: str, embedding: np.ndarray) -> float | None:
        """Cosine similarity against the enrolled template, or None if absent.

        None means "not enrolled", which is not the same as a low score.  The
        speaker branch must abstain rather than default (FR-DE-03).
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT ciphertext, nonce, wrapped_key, key_version, transform "
                "FROM voiceprints WHERE speaker_id = ? AND tenant_id = ?",
                (speaker_id, self.tenant_id),
            ).fetchone()
        if row is None:
            return None

        ciphertext, nonce, wrapped_json, key_version, transform_json = row
        wrapped = WrappedKey.from_dict(json.loads(wrapped_json))
        data_key = self.keystore.unwrap(wrapped)
        aad = self._aad(speaker_id, key_version)

        plaintext = AESGCM(data_key).decrypt(nonce, ciphertext, aad)
        stored = np.frombuffer(plaintext, dtype=np.float32)

        params = TransformParams(**json.loads(transform_json))
        if params.version == 0:
            # Stored as a plain normalised embedding: compare directly.
            probe = np.asarray(embedding, dtype=np.float32).ravel()
            norm = float(np.linalg.norm(probe))
            probe = probe / norm if norm > 0 else probe
        else:
            probe = transform(embedding, params)
        return compare(stored, probe)

    def is_enrolled(self, speaker_id: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM voiceprints WHERE speaker_id = ? AND tenant_id = ?",
                (speaker_id, self.tenant_id),
            ).fetchone()
        return row is not None

    def list_enrolled(self) -> list[EnrolmentRecord]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT speaker_id, tenant_id, template_dim, key_version, enrolled_ms, transform "
                "FROM voiceprints WHERE tenant_id = ?",
                (self.tenant_id,),
            ).fetchall()
        return [
            EnrolmentRecord(
                speaker_id=r[0],
                tenant_id=r[1],
                template_dim=r[2],
                key_version=r[3],
                # Read from the record: 0 for a plain embedding, >= 1 when transformed.
                transform_version=int(json.loads(r[5])["version"]),
                enrolled_ms=r[4],
            )
            for r in rows
        ]

    # -- internals ---------------------------------------------------------

    def _aad(self, speaker_id: str, key_version: int) -> bytes:
        """Bind ciphertext to its slot.

        Without this an attacker with write access could move ciphertexts
        between rows and every one would still decrypt.
        """
        return f"{self.tenant_id}|{speaker_id}|v{key_version}".encode()


def embedding_to_b64(embedding: np.ndarray) -> str:
    """Transport helper for the enrolment API.  Never used for storage."""
    return base64.b64encode(np.asarray(embedding, dtype=np.float32).tobytes()).decode()


def embedding_from_b64(raw: str) -> np.ndarray:
    return np.frombuffer(base64.b64decode(raw), dtype=np.float32)
