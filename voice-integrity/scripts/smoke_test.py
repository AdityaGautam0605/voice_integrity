#!/usr/bin/env python3
"""End-to-end smoke test with no downloads and no corpora.

Exercises every layer - ingest, VAD gate, windowing, scoring, policy, signing,
vault, audit log - using the stub detector and synthetic conversations.
Nothing here is a detection result; the point is to prove the plumbing is
correct before any weights exist.

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
from vif.common.types import Risk, Side, SpeakerStatus  # noqa: E402
from vif.crypto.auditlog import AuditLog  # noqa: E402
from vif.crypto.cancelable import compare, new_params, transform  # noqa: E402
from vif.crypto.keys import LocalKeyStore  # noqa: E402
from vif.crypto.vault import VoiceprintVault  # noqa: E402
from vif.crypto.verdict import VerdictSigner, VerdictVerifier, generate_keypair  # noqa: E402
from vif.eval.calibration import Calibrator, fit_platt  # noqa: E402
from vif.eval.metrics import sanity_check_random  # noqa: E402
from vif.serve.adapters.file import SyntheticAdapter  # noqa: E402
from vif.serve.detector import StubDetector  # noqa: E402
from vif.serve.scoring import Scorer  # noqa: E402
from vif.serve.session import CallSession  # noqa: E402
from vif.serve.vad import EnergyVAD  # noqa: E402

OK = "  ok  "
FAIL = " FAIL "


def check(label: str, condition: bool, detail: str = "") -> bool:
    print(f"[{OK if condition else FAIL}] {label}" + (f"  -  {detail}" if detail else ""))
    return bool(condition)


async def run_session(config, pipeline_floor_ms: float, seed: int = 1, turns: int = 16):
    """Drive one synthetic conversation all the way through the session layer."""
    messages: list[dict] = []

    async def collect(payload: dict) -> None:
        messages.append(payload)

    adapter = SyntheticAdapter(
        n_turns=turns, pipeline_floor_ms=pipeline_floor_ms, seed=seed, realtime=False
    )
    session = CallSession(
        session_id=f"smoke-{int(pipeline_floor_ms)}",
        config=config,
        detector=StubDetector(config.model.audio.window_samples),
        vad=EnergyVAD(config.model.audio.vad_frame),
        on_message=collect,
        calibrator=Calibrator({}),
    )
    if session.liveness is not None:
        session.liveness.set_rtt(await adapter.rtt_ms())

    await asyncio.gather(
        session.consume(adapter, Side.CALLER),
        session.consume(adapter, Side.AGENT),
    )
    return session, messages


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
            "thresholds ordered 0 < amber < red < 1",
            0 < config.model.scoring.amber_threshold < config.model.scoring.red_threshold < 1,
            f"amber={config.model.scoring.amber_threshold} red={config.model.scoring.red_threshold}",
        )
        passed &= check(
            "policy gates the action rather than the call",
            "TERMINATE" not in config.policy.actions.red.upper(),
            config.policy.actions.red,
        )

        # -- 2. metric harness (the gate) --------------------------------
        print("\n2. Metric harness")
        result = sanity_check_random()
        passed &= check(
            "random classifier reports EER near 50%",
            0.45 < result.eer < 0.55,
            result.summary(),
        )

        # -- 3. scoring --------------------------------------------------
        print("\n3. Scoring")
        rng = np.random.default_rng(0)
        labels = rng.integers(0, 2, size=4000)
        scores = np.where(labels == 0, rng.normal(1.5, 1.0, 4000), rng.normal(-1.5, 1.0, 4000))
        params = fit_platt(labels, scores, branch="spoof", split="dev")
        passed &= check(
            "Platt fit produces a positive slope",
            params.a > 0,
            f"llr = {params.a:.3f} * score + {params.b:.3f}",
        )

        scorer = Scorer(config.model.scoring, Calibrator({"spoof": params}))
        low = scorer.update_spoof(-4.0)
        scorer.reset()
        high = scorer.update_spoof(4.0)
        passed &= check(
            "probability is monotone in the detector score",
            0.0 < low < high < 1.0,
            f"p(-4)={low:.3f}  p(+4)={high:.3f}",
        )
        passed &= check(
            "bands follow the configured thresholds",
            scorer.risk() == Risk.RED,
            f"p={scorer.spoof_probability:.3f} -> {scorer.risk().value}",
        )
        passed &= check(
            "speaker branch abstains without enrolment",
            scorer.update_speaker(None) == SpeakerStatus.NOT_ENROLLED,
        )
        passed &= check(
            "low similarity reads as a mismatch, not a low score",
            scorer.update_speaker(0.05) == SpeakerStatus.MISMATCH,
        )

        # -- 4. live pipeline --------------------------------------------
        print("\n4. Live pipeline  (synthetic conversations)")
        session, messages = await run_session(config, pipeline_floor_ms=0.0)
        passed &= check(
            "stream messages were emitted",
            len(messages) > 0,
            f"{len(messages)} messages",
        )
        passed &= check(
            "windows scored off the event loop",
            session.stats.windows_scored > 0,
            f"{session.stats.windows_scored} windows, "
            f"{session.stats.mean_inference_ms:.1f} ms mean",
        )
        if messages:
            last = messages[-1]
            passed &= check(
                "output contract carries the expected fields",
                all(
                    k in last
                    for k in (
                        "session_id",
                        "sequence",
                        "speech_seconds",
                        "spoof_probability",
                        "risk",
                        "speaker_status",
                        "inference_ms",
                        "model_version",
                    )
                ),
                f"p={last['spoof_probability']:.3f} risk={last['risk']} "
                f"speech={last['speech_seconds']:.1f}s",
            )
            passed &= check(
                "probability is a probability",
                0.0 <= last["spoof_probability"] <= 1.0,
            )
            passed &= check(
                "speech seconds tracked separately from wall clock",
                last["speech_seconds"] > 0,
                f"{last['speech_seconds']:.1f}s of speech",
            )

        # -- 5. liveness (opt-in) ----------------------------------------
        print("\n5. Conversational liveness  (disabled by default)")
        passed &= check(
            "off unless explicitly enabled",
            session.liveness is None,
            "config.model.liveness.enabled = false",
        )
        live_config = config.model_copy(deep=True)
        live_config.model.liveness.enabled = True
        human, _ = await run_session(live_config, pipeline_floor_ms=0.0)
        machine, _ = await run_session(live_config, pipeline_floor_ms=320.0)
        hp, mp = human.finalise(), machine.finalise()
        passed &= check(
            "enabling it detects turn transitions",
            hp.liveness.n_transitions > 0 and mp.liveness.n_transitions > 0,
            f"human={hp.liveness.n_transitions} machine={mp.liveness.n_transitions}",
        )
        passed &= check(
            "machine conversation shows a higher response floor",
            (mp.liveness.caller_floor_ms or 0) > (hp.liveness.caller_floor_ms or 0),
            f"human={_ms(hp.liveness.caller_floor_ms)} machine={_ms(mp.liveness.caller_floor_ms)}",
        )
        passed &= check(
            "machine conversation loses caller-side overlap",
            (mp.liveness.caller_overlap_rate or 0.0) <= (hp.liveness.caller_overlap_rate or 0.0),
            f"human={_pct(hp.liveness.caller_overlap_rate)} "
            f"machine={_pct(mp.liveness.caller_overlap_rate)}",
        )

        # -- 6. policy ---------------------------------------------------
        print("\n6. Policy")
        decision = session.last_decision
        passed &= check(
            "a decision carries a readable reason",
            decision is not None and len(decision.reason) > 0,
            decision.reason if decision else "",
        )
        passed &= check(
            "action is one of the three configured outcomes",
            decision.action.value in ("PROCEED", "CHALLENGE", "GATE_ACTION"),
            decision.action.value,
        )

        # -- 7. verdict attestation --------------------------------------
        print("\n7. Verdict attestation")
        payload = session.finalise()
        keypair = generate_keypair()
        signer, verifier = VerdictSigner(keypair), VerdictVerifier(keypair.public_key)
        verdict = signer.sign(payload)

        ok, reason = verifier.verify(verdict)
        passed &= check("signed verdict verifies", ok, reason)

        tampered = verdict.model_copy(deep=True)
        tampered.payload.spoof_probability = 0.0
        ok_t, reason_t = verifier.verify(tampered, check_replay=False)
        passed &= check("tampered verdict is rejected", not ok_t, reason_t)

        ok_r, reason_r = verifier.verify(verdict)
        passed &= check("replayed verdict is rejected", not ok_r, reason_r)

        ok_n, reason_n = verifier.verify_or_fail_closed(None)
        passed &= check("missing verdict fails closed", not ok_n, reason_n)

        passed &= check(
            "verdict pins the model that produced it",
            verdict.payload.model_version != "",
            f"{verdict.payload.model_version} / {verdict.payload.model_checksum or 'no checksum'}",
        )

        # -- 8. audit log ------------------------------------------------
        print("\n8. Audit log")
        audit = AuditLog(workdir / "audit.db", signer=keypair)
        for _ in range(5):
            audit.append(signer.sign(session.finalise()))
        ok_chain, reason_chain = audit.verify_chain()
        passed &= check("hash chain intact", ok_chain, reason_chain)
        passed &= check(
            "Merkle checkpoints off by default",
            audit.checkpoint() is None,
            "hash chain alone detects any edit",
        )

        audit._conn.execute("UPDATE entries SET payload = '{\"t\":1}' WHERE seq = 2")
        audit._conn.commit()
        ok_bad, reason_bad = audit.verify_chain()
        passed &= check("tampering with an entry is detected", not ok_bad, reason_bad)
        audit.close()

        # -- 9. vault ----------------------------------------------------
        print("\n9. Voiceprint vault")
        keystore = LocalKeyStore(workdir / "keystore.json")
        vault = VoiceprintVault(workdir / "vp.db", keystore, use_cancelable=False)

        rng = np.random.default_rng(7)
        enrolled = rng.normal(size=192).astype(np.float32)
        same = enrolled + rng.normal(0, 0.15, 192).astype(np.float32)
        other = rng.normal(size=192).astype(np.float32)

        vault.enrol("ceo-001", enrolled)
        passed &= check("speaker enrolled", vault.is_enrolled("ceo-001"))
        passed &= check(
            "same speaker scores above a different one",
            vault.match("ceo-001", same) > vault.match("ceo-001", other),
            f"same={vault.match('ceo-001', same):.3f} other={vault.match('ceo-001', other):.3f}",
        )
        passed &= check("unenrolled speaker returns None", vault.match("nobody", enrolled) is None)
        passed &= check(
            "embeddings encrypted at rest",
            enrolled.tobytes() not in (workdir / "vp.db").read_bytes(),
            "raw bytes absent from the database file",
        )
        vault.revoke("ceo-001")
        passed &= check("revocation removes the template", not vault.is_enrolled("ceo-001"))
        vault.close()

        # -- 10. cancelable transform (opt-in) ---------------------------
        print("\n10. Cancelable templates  (disabled by default)")
        passed &= check(
            "off unless explicitly enabled",
            not config.security.cancelable_templates,
            "security.cancelable_templates = false",
        )
        params_a = new_params(in_dim=192, tenant="bank-a")
        t_a1, t_a2 = transform(enrolled, params_a), transform(same, params_a)
        passed &= check(
            "when enabled, distances survive the transform",
            compare(t_a1, t_a2) > 0.5,
            f"same-speaker similarity = {compare(t_a1, t_a2):.3f}",
        )
        passed &= check(
            "when enabled, rotating the seed revokes",
            abs(compare(t_a1, transform(enrolled, params_a.rotate()))) < 0.4,
        )

        # -- 11. teardown ------------------------------------------------
        print("\n11. Teardown")
        session.close()
        passed &= check("buffers zeroed at teardown", len(session.buffer) == 0, "no audio retained")
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


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
