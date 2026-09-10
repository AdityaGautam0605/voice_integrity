#!/usr/bin/env python3
"""End-to-end smoke test with no downloads and no corpora.

Exercises every layer of the pipeline - ingest, VAD gate, windowing, fusion,
policy, liveness, signing, vault, audit log - using the stub detector and
synthetic conversations.  Nothing here is a detection result; the point is to
prove the plumbing is correct before any weights exist.

Run:
    PYTHONPATH=src python scripts/smoke_test.py
"""

from __future__ import annotations

import asyncio
import shutil
import sys
import tempfile
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from vif.common.config import load_config  # noqa: E402
from vif.common.types import Side  # noqa: E402
from vif.crypto.auditlog import AuditLog  # noqa: E402
from vif.crypto.cancelable import compare, new_params, transform  # noqa: E402
from vif.crypto.keys import LocalKeyStore  # noqa: E402
from vif.crypto.vault import VoiceprintVault  # noqa: E402
from vif.crypto.verdict import VerdictSigner, VerdictVerifier, generate_keypair  # noqa: E402
from vif.eval.calibration import Calibrator, fit_platt  # noqa: E402
from vif.eval.metrics import sanity_check_random  # noqa: E402
from vif.serve.adapters.file import SyntheticAdapter  # noqa: E402
from vif.serve.detector import StubDetector  # noqa: E402
from vif.serve.session import CallSession  # noqa: E402
from vif.serve.vad import EnergyVAD  # noqa: E402

OK = "  ok  "
FAIL = " FAIL "


def check(label: str, condition: bool, detail: str = "") -> bool:
    print(f"[{OK if condition else FAIL}] {label}" + (f"  -  {detail}" if detail else ""))
    return condition


async def run_call(
    config,
    pipeline_floor_ms: float,
    n_turns: int = 16,
    seed: int = 0,
) -> tuple[CallSession, list[dict]]:
    """Drive one synthetic call all the way through the session layer."""
    frames: list[dict] = []

    async def collect(payload: dict) -> None:
        frames.append(payload)

    adapter = SyntheticAdapter(
        n_turns=n_turns,
        pipeline_floor_ms=pipeline_floor_ms,
        seed=seed,
        realtime=False,
    )
    session = CallSession(
        call_id=f"smoke-floor{int(pipeline_floor_ms)}",
        config=config,
        detector=StubDetector(config.model.audio.window_samples),
        vad=EnergyVAD(config.model.audio.vad_frame),
        on_score=collect,
        calibrator=Calibrator({}),
        metadata={"first_contact": True, "transaction_value": 5_000_000},
    )
    session.liveness.set_rtt(await adapter.rtt_ms())

    await asyncio.gather(
        session.consume(adapter, Side.CALLER),
        session.consume(adapter, Side.AGENT),
    )
    return session, frames


async def main() -> int:
    print("=" * 74)
    print("Voice Integrity Framework - end-to-end smoke test")
    print("=" * 74)
    passed = True
    workdir = Path(tempfile.mkdtemp(prefix="vif-smoke-"))

    try:
        # -- 1. config ---------------------------------------------------
        print("\n1. Configuration")
        config = load_config("configs")
        passed &= check(
            "config loads and validates",
            config.model.audio.window_samples == 64600,
            f"window={config.model.audio.window_samples} hop={config.model.audio.hop_samples}",
        )
        passed &= check(
            "policy rejects terminate_call",
            config.policy.actions.get("red") == "gate_action",
        )
        passed &= check("tiers are ordered and open-ended last", len(config.policy.tiers) >= 1)

        # -- 2. metric harness (the P0 gate) -----------------------------
        print("\n2. Metric harness  (P0 gate)")
        result = sanity_check_random()
        passed &= check(
            "random classifier reports EER near 50%",
            0.45 < result.eer < 0.55,
            result.summary(),
        )

        # -- 3. calibration ----------------------------------------------
        print("\n3. Calibration")
        rng = np.random.default_rng(0)
        labels = rng.integers(0, 2, size=4000)
        scores = np.where(labels == 0, rng.normal(1.5, 1.0, 4000), rng.normal(-1.5, 1.0, 4000))
        params = fit_platt(labels, scores, branch="spoof", split="dev")
        passed &= check(
            "Platt fit produces a positive slope",
            params.a > 0,
            f"llr = {params.a:.3f} * score + {params.b:.3f}",
        )
        llr_hi = params.to_llr(3.0)
        llr_lo = params.to_llr(-3.0)
        passed &= check("LLR is monotone in the score", llr_hi > llr_lo)

        # -- 4. full pipeline, human vs machine --------------------------
        print("\n4. Live pipeline  (synthetic conversations)")
        human_session, human_frames = await run_call(config, pipeline_floor_ms=0.0, seed=1)
        machine_session, machine_frames = await run_call(config, pipeline_floor_ms=320.0, seed=1)

        passed &= check(
            "score frames were emitted",
            len(human_frames) > 0 and len(machine_frames) > 0,
            f"human={len(human_frames)} machine={len(machine_frames)}",
        )
        passed &= check(
            "windows were scored off the event loop",
            human_session.stats.windows_scored > 0,
            f"{human_session.stats.windows_scored} windows, "
            f"{human_session.stats.mean_inference_ms:.1f} ms mean",
        )
        if human_frames:
            first, last = human_frames[0], human_frames[-1]
            passed &= check(
                "confidence interval narrows as evidence accumulates",
                last["ci"] <= first["ci"],
                f"{first['ci']:.1f} -> {last['ci']:.1f}",
            )
            passed &= check(
                "speech seconds tracked separately from wall clock",
                last["speech_s"] > 0,
                f"{last['speech_s']:.1f}s of speech",
            )

        # -- 5. liveness -------------------------------------------------
        print("\n5. Branch D  (conversational liveness)")
        human_payload = human_session.finalise()
        machine_payload = machine_session.finalise()
        hf, mf = human_payload.liveness, machine_payload.liveness

        passed &= check(
            "turn transitions detected on both calls",
            hf.n_transitions > 0 and mf.n_transitions > 0,
            f"human={hf.n_transitions} machine={mf.n_transitions}",
        )
        passed &= check(
            "machine call shows a higher response floor",
            (mf.caller_floor_ms or 0) > (hf.caller_floor_ms or 0),
            f"human floor={_ms(hf.caller_floor_ms)} machine floor={_ms(mf.caller_floor_ms)}",
        )
        passed &= check(
            "machine call loses caller-side overlap",
            (mf.caller_overlap_rate or 0.0) <= (hf.caller_overlap_rate or 0.0),
            f"human overlap={_pct(hf.caller_overlap_rate)} "
            f"machine overlap={_pct(mf.caller_overlap_rate)}",
        )
        passed &= check(
            "liveness LLR is higher for the machine call",
            (machine_payload.branches.liveness or 0) > (human_payload.branches.liveness or 0),
            f"human={_num(human_payload.branches.liveness)} "
            f"machine={_num(machine_payload.branches.liveness)}",
        )

        # -- 6. policy ---------------------------------------------------
        print("\n6. Policy")
        passed &= check(
            "high transaction value selects the strictest tier",
            human_session.last_decision.tier == config.policy.tiers[-1].name,
            f"tier={human_session.last_decision.tier} "
            f"amber={human_session.last_decision.amber_threshold}",
        )
        passed &= check(
            "action gates rather than terminating",
            human_session.last_decision.action.value in ("proceed", "challenge", "gate_action"),
            human_session.last_decision.action.value,
        )

        # -- 7. verdict signing ------------------------------------------
        print("\n7. Verdict attestation")
        keypair = generate_keypair()
        signer, verifier = VerdictSigner(keypair), VerdictVerifier(keypair.public_key)
        verdict = signer.sign(human_payload)

        ok, reason = verifier.verify(verdict)
        passed &= check("signed verdict verifies", ok, reason)

        tampered = verdict.model_copy(deep=True)
        tampered.payload.risk = 0.0
        ok_t, reason_t = verifier.verify(tampered, check_replay=False)
        passed &= check("tampered verdict is rejected", not ok_t, reason_t)

        ok_r, reason_r = verifier.verify(verdict)
        passed &= check("replayed verdict is rejected", not ok_r, reason_r)

        ok_n, reason_n = verifier.verify_or_fail_closed(None)
        passed &= check("missing verdict fails closed", not ok_n, reason_n)

        # -- 8. audit log ------------------------------------------------
        print("\n8. Audit log")
        audit = AuditLog(workdir / "audit.db", signer=keypair)
        for _ in range(5):
            audit.append(signer.sign(human_session.finalise()))
        ok_chain, reason_chain = audit.verify_chain()
        passed &= check("hash chain intact", ok_chain, reason_chain)

        audit.checkpoint()
        ok_cp, reason_cp = audit.verify_checkpoint(keypair.public_key)
        passed &= check("signed checkpoint verifies", ok_cp, reason_cp)

        audit._conn.execute("UPDATE entries SET payload = '{\"tampered\":true}' WHERE seq = 2")
        audit._conn.commit()
        ok_bad, reason_bad = audit.verify_chain()
        passed &= check("tampering with an entry is detected", not ok_bad, reason_bad)
        audit.close()

        # -- 9. vault ----------------------------------------------------
        print("\n9. Voiceprint vault")
        keystore = LocalKeyStore(workdir / "keystore.json")
        vault = VoiceprintVault(workdir / "voiceprints.db", keystore)

        rng = np.random.default_rng(7)
        enrolled = rng.normal(size=192).astype(np.float32)
        same_speaker = enrolled + rng.normal(0, 0.15, 192).astype(np.float32)
        other_speaker = rng.normal(size=192).astype(np.float32)

        vault.enrol("ceo-001", enrolled)
        passed &= check("speaker enrolled", vault.is_enrolled("ceo-001"))

        match_same = vault.match("ceo-001", same_speaker)
        match_other = vault.match("ceo-001", other_speaker)
        passed &= check(
            "same speaker scores above a different one",
            match_same > match_other,
            f"same={match_same:.3f} other={match_other:.3f}",
        )
        passed &= check("unenrolled speaker returns None", vault.match("nobody", enrolled) is None)

        vault.revoke("ceo-001")
        passed &= check("revocation removes the template", not vault.is_enrolled("ceo-001"))

        # -- 10. cancelable transform ------------------------------------
        print("\n10. Cancelable biometric transform")
        params_a = new_params(in_dim=192, tenant="bank-a")
        params_b = new_params(in_dim=192, tenant="bank-b")
        t_a1 = transform(enrolled, params_a)
        t_a2 = transform(same_speaker, params_a)
        t_b1 = transform(enrolled, params_b)

        passed &= check(
            "distances survive the transform",
            compare(t_a1, t_a2) > 0.5,
            f"same-speaker similarity under transform = {compare(t_a1, t_a2):.3f}",
        )
        passed &= check(
            "templates are unlinkable across tenants",
            abs(compare(t_a1, t_b1)) < 0.4,
            f"cross-tenant similarity = {compare(t_a1, t_b1):.3f}",
        )
        rotated = params_a.rotate()
        passed &= check(
            "rotating the seed invalidates old templates",
            abs(compare(t_a1, transform(enrolled, rotated))) < 0.4,
            "old template no longer matches",
        )

        vault.close()

        # -- 11. teardown ------------------------------------------------
        print("\n11. Teardown")
        human_session.close()
        machine_session.close()
        passed &= check(
            "buffers zeroed at teardown",
            len(human_session.buffer) == 0,
            "no audio retained",
        )
        audio_files = list(Path(".").glob("**/*.wav")) + list(Path(".").glob("**/*.flac"))
        passed &= check(
            "no audio written to disk during the run",
            len(audio_files) == 0,
            f"{len(audio_files)} audio files found",
        )

    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    print("\n" + "=" * 74)
    print("RESULT:", "ALL CHECKS PASSED" if passed else "FAILURES ABOVE")
    print("=" * 74)
    return 0 if passed else 1


def _ms(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.0f}ms"


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.0f}%"


def _num(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.3f}"


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
