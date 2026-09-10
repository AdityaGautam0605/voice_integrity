"""Key management.

Envelope encryption with versioned keys: data is encrypted under a per-record
data key, which is itself wrapped by a key-encrypting key.  Rotating the KEK
therefore does not require re-encrypting the store or re-enrolling anybody.

`LocalKeyStore` is a development stand-in.  In production the KEK lives in a
KMS or HSM and never leaves it, and - the part that actually matters -
whoever can read the database must not be able to read the keys.  Encryption
where one operator holds both buys compliance paperwork and very little
security (SEC-11).
"""

from __future__ import annotations

import json
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.hashes import SHA256
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from vif.common.logging import get_logger

log = get_logger(__name__)

KEY_BYTES = 32  # AES-256
NONCE_BYTES = 12  # 96-bit, the GCM standard


@dataclass
class WrappedKey:
    """A data key sealed under a specific KEK version."""

    ciphertext: bytes
    nonce: bytes
    key_version: int

    def to_dict(self) -> dict:
        import base64

        return {
            "ciphertext": base64.b64encode(self.ciphertext).decode(),
            "nonce": base64.b64encode(self.nonce).decode(),
            "key_version": self.key_version,
        }

    @classmethod
    def from_dict(cls, blob: dict) -> WrappedKey:
        import base64

        return cls(
            ciphertext=base64.b64decode(blob["ciphertext"]),
            nonce=base64.b64decode(blob["nonce"]),
            key_version=int(blob["key_version"]),
        )


class KeyStore(ABC):
    """Interface a real KMS or HSM would implement."""

    @abstractmethod
    def current_version(self) -> int: ...

    @abstractmethod
    def wrap(self, data_key: bytes) -> WrappedKey: ...

    @abstractmethod
    def unwrap(self, wrapped: WrappedKey) -> bytes: ...

    def new_data_key(self) -> tuple[bytes, WrappedKey]:
        """Generate a fresh data key and its wrapped form."""
        data_key = os.urandom(KEY_BYTES)
        return data_key, self.wrap(data_key)


class LocalKeyStore(KeyStore):
    """File-backed KEK store for development and the demo.

    Not a substitute for a KMS.  It exists so the pipeline runs end to end
    offline, and it refuses to be silent about what it is.
    """

    def __init__(self, path: str | Path, master_key: bytes | None = None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._keks: dict[int, bytes] = {}
        self._version = 0

        if master_key is not None:
            self._derive_from_master(master_key)
        elif self.path.exists():
            self._load()
        else:
            self._rotate_internal()
            self._save()
            log.warning(
                "generated a NEW local key store at %s - development only, "
                "production keys belong in a KMS or HSM",
                self.path,
            )

    def _derive_from_master(self, master_key: bytes) -> None:
        """Deterministic KEK from a master secret, via HKDF."""
        kdf = HKDF(algorithm=SHA256(), length=KEY_BYTES, salt=None, info=b"vif-kek-v1")
        self._keks = {1: kdf.derive(master_key)}
        self._version = 1

    def _load(self) -> None:
        import base64

        blob = json.loads(self.path.read_text(encoding="utf-8"))
        self._keks = {int(k): base64.b64decode(v) for k, v in blob["keks"].items()}
        self._version = int(blob["current_version"])

    def _save(self) -> None:
        import base64

        blob = {
            "current_version": self._version,
            "keks": {str(k): base64.b64encode(v).decode() for k, v in self._keks.items()},
        }
        self.path.write_text(json.dumps(blob, indent=2), encoding="utf-8")
        try:
            os.chmod(self.path, 0o600)
        except OSError:  # pragma: no cover - Windows
            pass

    def _rotate_internal(self) -> int:
        self._version += 1
        self._keks[self._version] = os.urandom(KEY_BYTES)
        return self._version

    def rotate(self) -> int:
        """Issue a new KEK version.

        Old versions are retained so existing records stay readable.  This is
        why every record carries its `key_version` (SEC-10).
        """
        version = self._rotate_internal()
        self._save()
        log.info("rotated KEK to version %d (%d versions retained)", version, len(self._keks))
        return version

    def current_version(self) -> int:
        return self._version

    def wrap(self, data_key: bytes) -> WrappedKey:
        nonce = os.urandom(NONCE_BYTES)
        kek = self._keks[self._version]
        ciphertext = AESGCM(kek).encrypt(nonce, data_key, b"vif-dek-wrap")
        return WrappedKey(ciphertext=ciphertext, nonce=nonce, key_version=self._version)

    def unwrap(self, wrapped: WrappedKey) -> bytes:
        kek = self._keks.get(wrapped.key_version)
        if kek is None:
            raise KeyError(
                f"KEK version {wrapped.key_version} is not available. "
                "Retiring a key version makes every record sealed under it unreadable."
            )
        return AESGCM(kek).decrypt(wrapped.nonce, wrapped.ciphertext, b"vif-dek-wrap")
