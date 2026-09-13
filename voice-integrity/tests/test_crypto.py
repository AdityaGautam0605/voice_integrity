"""Cryptography: verdicts, vault, cancelable transform, audit log.

Every test here has a negative case.  A security control that has only been
tested on the happy path has not been tested.
"""

from __future__ import annotations

import numpy as np
import pytest

from vif.common.types import Action, Risk, SpeakerStatus, VerdictPayload
from vif.crypto.auditlog import AuditLog, merkle_root
from vif.crypto.cancelable import compare, new_params, transform
from vif.crypto.keys import LocalKeyStore
from vif.crypto.vault import VoiceprintVault
from vif.crypto.verdict import (
    VerdictSigner,
    VerdictVerifier,
    canonical_encode,
    generate_keypair,
    load_private_key,
    save_keypair,
)


def make_payload(session_id: str = "s-1", probability: float = 0.87) -> VerdictPayload:
    return VerdictPayload(
        session_id=session_id,
        spoof_probability=probability,
        risk=Risk.RED,
        speaker_status=SpeakerStatus.MISMATCH,
        speaker_similarity=0.11,
        action=Action.GATE_ACTION,
        speech_seconds=18.5,
        windows_scored=15,
        model_version="aasist-codec_robust-e12",
        model_checksum="a1b2c3d4e5f6",
        policy_version="policy-1.0.0",
    )


class TestVerdict:
    def test_signed_verdict_verifies(self):
        pair = generate_keypair()
        verdict = VerdictSigner(pair).sign(make_payload())
        ok, reason = VerdictVerifier(pair.public_key).verify(verdict)
        assert ok, reason

    def test_signer_injects_a_nonce(self):
        verdict = VerdictSigner(generate_keypair()).sign(make_payload())
        assert len(verdict.payload.nonce) == 32

    def test_tampering_with_the_probability_is_detected(self):
        """The attack this defends against: flip the score, keep everything else."""
        pair = generate_keypair()
        verdict = VerdictSigner(pair).sign(make_payload(probability=0.95))
        verdict.payload.spoof_probability = 0.0
        ok, reason = VerdictVerifier(pair.public_key).verify(verdict, check_replay=False)
        assert not ok and "does not verify" in reason

    def test_tampering_with_the_band_is_detected(self):
        pair = generate_keypair()
        verdict = VerdictSigner(pair).sign(make_payload())
        verdict.payload.risk = Risk.GREEN
        ok, _ = VerdictVerifier(pair.public_key).verify(verdict, check_replay=False)
        assert not ok

    def test_wrong_key_is_rejected(self):
        verdict = VerdictSigner(generate_keypair()).sign(make_payload())
        ok, _ = VerdictVerifier(generate_keypair().public_key).verify(verdict)
        assert not ok

    def test_replay_is_rejected(self):
        pair = generate_keypair()
        verifier = VerdictVerifier(pair.public_key)
        verdict = VerdictSigner(pair).sign(make_payload())
        assert verifier.verify(verdict)[0]
        ok, reason = verifier.verify(verdict)
        assert not ok and "replayed" in reason

    def test_stale_verdict_is_rejected(self):
        pair = generate_keypair()
        payload = make_payload()
        payload.timestamp -= 10 * 60 * 1000
        verdict = VerdictSigner(pair).sign(payload)
        ok, reason = VerdictVerifier(pair.public_key).verify(verdict)
        assert not ok and "stale" in reason

    def test_missing_verdict_fails_closed(self):
        """Absence of evidence is not evidence of safety."""
        ok, reason = VerdictVerifier(generate_keypair().public_key).verify_or_fail_closed(None)
        assert not ok and "failing closed" in reason

    def test_unsigned_verdict_is_rejected(self):
        from vif.common.types import Verdict

        verdict = Verdict(payload=make_payload(), signature="")
        ok, reason = VerdictVerifier(generate_keypair().public_key).verify(verdict)
        assert not ok and "no signature" in reason

    def test_canonical_encoding_is_stable(self):
        payload = make_payload()
        assert canonical_encode(payload) == canonical_encode(payload.model_copy(deep=True))

    def test_keypair_roundtrip(self, tmp_path):
        pair = generate_keypair()
        save_keypair(pair, tmp_path / "k.pem", tmp_path / "k.pub.pem")
        reloaded = load_private_key(tmp_path / "k.pem")
        assert reloaded.key_id == pair.key_id
        verdict = VerdictSigner(reloaded).sign(make_payload())
        assert VerdictVerifier(pair.public_key).verify(verdict)[0]


class TestCancelable:
    def test_distances_survive_the_transform(self):
        rng = np.random.default_rng(0)
        base = rng.normal(size=192).astype(np.float32)
        near = base + rng.normal(0, 0.1, 192).astype(np.float32)
        params = new_params(in_dim=192)
        assert compare(transform(base, params), transform(near, params)) > 0.7

    def test_different_speakers_stay_apart(self):
        rng = np.random.default_rng(1)
        params = new_params(in_dim=192)
        a = transform(rng.normal(size=192).astype(np.float32), params)
        b = transform(rng.normal(size=192).astype(np.float32), params)
        assert abs(compare(a, b)) < 0.5

    def test_rotation_revokes(self):
        """The property that matters most for irrevocable biometrics."""
        rng = np.random.default_rng(2)
        embedding = rng.normal(size=192).astype(np.float32)
        params = new_params(in_dim=192)
        old = transform(embedding, params)
        new = transform(embedding, params.rotate())
        assert abs(compare(old, new)) < 0.5

    def test_tenants_are_unlinkable(self):
        rng = np.random.default_rng(3)
        embedding = rng.normal(size=192).astype(np.float32)
        a = transform(embedding, new_params(in_dim=192, tenant="bank-a"))
        b = transform(embedding, new_params(in_dim=192, tenant="bank-b"))
        assert abs(compare(a, b)) < 0.5

    def test_dimension_mismatch_raises(self):
        with pytest.raises(ValueError):
            transform(np.zeros(128, dtype=np.float32), new_params(in_dim=192))


class TestVault:
    def _vault(self, tmp_path):
        keystore = LocalKeyStore(tmp_path / "keystore.json")
        return VoiceprintVault(tmp_path / "vp.db", keystore)

    def test_enrol_and_match(self, tmp_path):
        vault = self._vault(tmp_path)
        rng = np.random.default_rng(0)
        enrolled = rng.normal(size=192).astype(np.float32)
        vault.enrol("ceo", enrolled)
        same = vault.match("ceo", enrolled + rng.normal(0, 0.1, 192).astype(np.float32))
        other = vault.match("ceo", rng.normal(size=192).astype(np.float32))
        assert same > other
        vault.close()

    def test_unenrolled_returns_none_not_zero(self, tmp_path):
        """Abstention is not the same as a low score."""
        vault = self._vault(tmp_path)
        assert vault.match("nobody", np.zeros(192, dtype=np.float32)) is None
        vault.close()

    def test_revocation_removes_the_template(self, tmp_path):
        vault = self._vault(tmp_path)
        vault.enrol("x", np.random.default_rng(0).normal(size=192).astype(np.float32))
        vault.revoke("x")
        assert not vault.is_enrolled("x")
        vault.close()

    def test_key_rotation_keeps_old_records_readable(self, tmp_path):
        keystore = LocalKeyStore(tmp_path / "keystore.json")
        vault = VoiceprintVault(tmp_path / "vp.db", keystore)
        embedding = np.random.default_rng(0).normal(size=192).astype(np.float32)
        vault.enrol("x", embedding)
        keystore.rotate()
        assert vault.match("x", embedding) is not None  # old KEK retained
        vault.close()

    def test_raw_embedding_is_never_stored(self, tmp_path):
        """The stored form must not be the biometric."""
        vault = self._vault(tmp_path)
        embedding = np.random.default_rng(0).normal(size=192).astype(np.float32)
        vault.enrol("x", embedding)
        blob = (tmp_path / "vp.db").read_bytes()
        assert embedding.tobytes() not in blob
        vault.close()


class TestAuditLog:
    def _log(self, tmp_path):
        pair = generate_keypair()
        return AuditLog(tmp_path / "audit.db", signer=pair, merkle_checkpoints=True), pair

    def test_chain_intact_after_appends(self, tmp_path):
        log, pair = self._log(tmp_path)
        signer = VerdictSigner(pair)
        for i in range(6):
            log.append(signer.sign(make_payload(session_id=f"s{i}")))
        ok, reason = log.verify_chain()
        assert ok, reason
        log.close()

    def test_altered_entry_is_detected(self, tmp_path):
        log, pair = self._log(tmp_path)
        signer = VerdictSigner(pair)
        for i in range(4):
            log.append(signer.sign(make_payload(session_id=f"s{i}")))
        log._conn.execute("UPDATE entries SET payload = '{\"x\":1}' WHERE seq = 1")
        log._conn.commit()
        ok, reason = log.verify_chain()
        assert not ok and "altered" in reason
        log.close()

    def test_deleted_entry_is_detected(self, tmp_path):
        log, pair = self._log(tmp_path)
        signer = VerdictSigner(pair)
        for i in range(5):
            log.append(signer.sign(make_payload(session_id=f"s{i}")))
        log._conn.execute("DELETE FROM entries WHERE seq = 2")
        log._conn.commit()
        ok, _ = log.verify_chain()
        assert not ok
        log.close()

    def test_signed_checkpoint_verifies(self, tmp_path):
        log, pair = self._log(tmp_path)
        signer = VerdictSigner(pair)
        for i in range(3):
            log.append(signer.sign(make_payload(session_id=f"s{i}")))
        assert log.checkpoint() is not None
        ok, reason = log.verify_checkpoint(pair.public_key)
        assert ok, reason
        log.close()

    def test_personal_data_is_refused(self, tmp_path):
        """The log holds decisions, never content."""
        log, _ = self._log(tmp_path)
        with pytest.raises(ValueError, match="refusing to log"):
            log._reject_personal_data({"payload": {"embedding": [0.1, 0.2]}})
        log.close()

    def test_merkle_root_changes_with_content(self):
        assert merkle_root(["a", "b", "c"]) != merkle_root(["a", "b", "d"])

    def test_merkle_root_handles_odd_counts(self):
        assert len(merkle_root(["a", "b", "c"])) == 64
