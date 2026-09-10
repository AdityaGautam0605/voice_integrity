"""Verdict attestation.

The highest security value per line of code in this project, and the piece
most often forgotten.

The system's entire output is a number that tells a bank whether to move
money.  If an attacker can forge or suppress that number, they never need to
touch the audio at all - they attack the cheapest link, and an unsigned JSON
field on an internal network is by far the cheapest.  No amount of model
accuracy compensates for that.

Signed payloads carry a nonce and a timestamp so an old "safe" verdict cannot
be replayed, and a model version so every decision is auditable to a specific
model.  Verification failure means elevated risk, never absence of risk: a
system that fails open can be disabled with a cut cable.
"""

from __future__ import annotations

import base64
import json
import secrets
import time
from dataclasses import dataclass
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from vif.common.logging import get_logger
from vif.common.types import Verdict, VerdictPayload

log = get_logger(__name__)

MAX_VERDICT_AGE_MS = 5 * 60 * 1000  # replay window


def canonical_encode(payload: VerdictPayload) -> bytes:
    """Deterministic bytes for signing.

    Sorted keys, no whitespace, explicit UTF-8.  Both signer and verifier must
    produce byte-identical output or valid verdicts start failing at random -
    so this function is the single definition, used by both sides.
    """
    data = payload.model_dump(mode="json")
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


@dataclass
class SigningKeyPair:
    private_key: Ed25519PrivateKey
    key_id: str

    @property
    def public_key(self) -> Ed25519PublicKey:
        return self.private_key.public_key()

    def public_bytes(self) -> bytes:
        return self.public_key.public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )


def generate_keypair(key_id: str | None = None) -> SigningKeyPair:
    private_key = Ed25519PrivateKey.generate()
    if key_id is None:
        raw = private_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        key_id = base64.urlsafe_b64encode(raw).decode()[:16]
    return SigningKeyPair(private_key=private_key, key_id=key_id)


def save_keypair(pair: SigningKeyPair, private_path: str | Path, public_path: str | Path) -> None:
    private_path, public_path = Path(private_path), Path(public_path)
    private_path.parent.mkdir(parents=True, exist_ok=True)

    private_path.write_bytes(
        pair.private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    try:
        import os

        os.chmod(private_path, 0o600)
    except OSError:  # pragma: no cover - Windows
        pass

    public_path.write_bytes(
        pair.public_key.public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    (public_path.parent / "key_id.txt").write_text(pair.key_id, encoding="utf-8")
    log.info("wrote signing key pair (key_id=%s)", pair.key_id)


def load_private_key(path: str | Path, key_id: str | None = None) -> SigningKeyPair:
    data = Path(path).read_bytes()
    key = serialization.load_pem_private_key(data, password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise TypeError("expected an Ed25519 private key")
    if key_id is None:
        id_file = Path(path).parent / "key_id.txt"
        key_id = id_file.read_text(encoding="utf-8").strip() if id_file.exists() else "unknown"
    return SigningKeyPair(private_key=key, key_id=key_id)


def load_public_key(path: str | Path) -> Ed25519PublicKey:
    key = serialization.load_pem_public_key(Path(path).read_bytes())
    if not isinstance(key, Ed25519PublicKey):
        raise TypeError("expected an Ed25519 public key")
    return key


class VerdictSigner:
    def __init__(self, keypair: SigningKeyPair):
        self.keypair = keypair

    def sign(self, payload: VerdictPayload) -> Verdict:
        """Seal a payload.  A nonce is injected if the caller did not set one."""
        if not payload.nonce:
            payload.nonce = secrets.token_hex(16)
        signature = self.keypair.private_key.sign(canonical_encode(payload))
        return Verdict(
            payload=payload,
            alg="Ed25519",
            key_id=self.keypair.key_id,
            signature=base64.b64encode(signature).decode(),
        )


class VerdictVerifier:
    """What the consuming business system runs before acting.

    Shipped here so the bank side and the scoring side cannot drift: both use
    the same canonical encoding and the same freshness rule.
    """

    def __init__(self, public_key: Ed25519PublicKey, max_age_ms: int = MAX_VERDICT_AGE_MS):
        self.public_key = public_key
        self.max_age_ms = max_age_ms
        self._seen_nonces: set[str] = set()

    def verify(self, verdict: Verdict, check_replay: bool = True) -> tuple[bool, str]:
        """Return (ok, reason).

        Never raises.  A verifier that throws is a verifier a caller wraps in
        a bare except, and then everything passes.
        """
        if verdict.alg != "Ed25519":
            return False, f"unsupported algorithm: {verdict.alg}"
        if not verdict.signature:
            return False, "verdict carries no signature"

        try:
            signature = base64.b64decode(verdict.signature)
            self.public_key.verify(signature, canonical_encode(verdict.payload))
        except InvalidSignature:
            return False, "signature does not verify - payload was altered or forged"
        except Exception as exc:  # noqa: BLE001
            return False, f"malformed signature: {exc}"

        age_ms = int(time.time() * 1000) - verdict.payload.ts_ms
        if age_ms > self.max_age_ms:
            return False, f"verdict is stale ({age_ms / 1000:.0f}s old) - possible replay"
        if age_ms < -60_000:
            return False, "verdict timestamp is in the future - clock skew or forgery"

        if check_replay:
            nonce = verdict.payload.nonce
            if nonce in self._seen_nonces:
                return False, "nonce already seen - replayed verdict"
            self._seen_nonces.add(nonce)

        return True, "ok"

    def verify_or_fail_closed(self, verdict: Verdict | None) -> tuple[bool, str]:
        """The rule a consumer must implement.

        A missing verdict is treated exactly like an invalid one.  Absence of
        evidence is not evidence of safety (SEC-01).
        """
        if verdict is None:
            return False, "no verdict received - failing closed"
        return self.verify(verdict)


def verdict_to_json(verdict: Verdict) -> str:
    return json.dumps(verdict.model_dump(mode="json"), indent=2, ensure_ascii=False)


def verdict_from_json(raw: str) -> Verdict:
    return Verdict(**json.loads(raw))
