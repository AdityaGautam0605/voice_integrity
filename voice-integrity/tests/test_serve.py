"""Serving layer: ring buffer, VAD, scoring, policy, liveness, session."""

from __future__ import annotations

import asyncio

import numpy as np
import pytest

from vif.common.config import (
    ActionConfig,
    AudioConfig,
    LivenessConfig,
    PolicyConfig,
    ScoringConfig,
    load_config,
)
from vif.common.types import Risk, Side, SpeakerStatus, TurnEvent, TurnEventKind
from vif.eval.calibration import Calibrator, PlattParams
from vif.serve.adapters.file import SyntheticAdapter
from vif.serve.detector import StubDetector
from vif.serve.liveness import (
    LivenessBranch,
    Utterance,
    build_transitions,
    compute_features,
    features_to_llr,
)
from vif.serve.policy import PolicyEngine
from vif.serve.ringbuffer import SpeechRingBuffer
from vif.serve.scoring import Scorer, logit, risk_from_probability, sigmoid
from vif.serve.session import CallSession
from vif.serve.vad import EnergyVAD


class TestRingBuffer:
    def test_window_needs_a_full_hop(self):
        buf = SpeechRingBuffer(window_samples=1000, hop_samples=100)
        buf.append(np.ones(1000, dtype=np.float32))
        assert buf.take_window() is not None
        assert buf.take_window() is None
        buf.append(np.ones(100, dtype=np.float32))
        assert buf.take_window() is not None

    def test_window_length_is_exact(self):
        buf = SpeechRingBuffer(window_samples=64600, hop_samples=16000)
        buf.append(np.ones(80000, dtype=np.float32))
        window = buf.take_window()
        assert window is not None and len(window) == 64600

    def test_capacity_is_bounded(self):
        buf = SpeechRingBuffer(window_samples=1000, hop_samples=100)
        for _ in range(50):
            buf.append(np.ones(500, dtype=np.float32))
        assert len(buf) <= 1000  # the privacy guarantee, as a data structure

    def test_speech_seconds_tracks_only_speech(self):
        buf = SpeechRingBuffer(window_samples=1000, hop_samples=100)
        buf.append(np.ones(16000, dtype=np.float32))
        assert buf.speech_seconds(16000) == pytest.approx(1.0)

    def test_zeroise_clears(self):
        buf = SpeechRingBuffer(window_samples=1000, hop_samples=100)
        buf.append(np.ones(1000, dtype=np.float32))
        buf.zeroise()
        assert len(buf) == 0

    def test_hop_larger_than_window_rejected(self):
        with pytest.raises(ValueError):
            SpeechRingBuffer(window_samples=100, hop_samples=200)


class TestVAD:
    def test_silence_is_not_speech(self):
        vad = EnergyVAD(frame_size=512)
        for _ in range(10):
            vad.is_speech(np.zeros(512, dtype=np.float32))
        assert not vad.is_speech(np.zeros(512, dtype=np.float32))

    def test_loud_audio_after_silence_is_speech(self):
        vad = EnergyVAD(frame_size=512)
        for _ in range(20):
            vad.is_speech(np.zeros(512, dtype=np.float32))
        tone = 0.5 * np.sin(np.linspace(0, 50, 512)).astype(np.float32)
        assert vad.is_speech(tone)


class TestScoring:
    def _scorer(self, **kw):
        config = ScoringConfig(**kw)
        return Scorer(config, Calibrator({"spoof": PlattParams(a=1.0, b=0.0, fitted_on="dev")}))

    def test_sigmoid_logit_roundtrip(self):
        for p in (0.01, 0.25, 0.5, 0.75, 0.99):
            assert sigmoid(logit(p)) == pytest.approx(p, abs=1e-6)

    def test_probability_is_monotone(self):
        low = self._scorer().update_spoof(-3.0)
        high = self._scorer().update_spoof(3.0)
        assert 0.0 < low < high < 1.0

    def test_bands_follow_configured_thresholds(self):
        config = ScoringConfig(amber_threshold=0.5, red_threshold=0.8)
        assert risk_from_probability(0.2, config) == Risk.GREEN
        assert risk_from_probability(0.6, config) == Risk.AMBER
        assert risk_from_probability(0.9, config) == Risk.RED

    def test_thresholds_must_be_ordered(self):
        with pytest.raises(ValueError):
            ScoringConfig(amber_threshold=0.9, red_threshold=0.5)

    def test_smoothing_settles_a_jittery_score(self):
        """A single odd window must not swing the reported probability."""
        scorer = self._scorer(smoothing_windows=5)
        for _ in range(5):
            scorer.update_spoof(2.0)
        steady = scorer.spoof_probability
        scorer.update_spoof(-2.0)  # one contradictory window
        assert abs(scorer.spoof_probability - steady) < 0.35

    def test_smoothing_of_one_reports_the_raw_window(self):
        scorer = self._scorer(smoothing_windows=1)
        scorer.update_spoof(2.0)
        scorer.update_spoof(-2.0)
        assert scorer.spoof_probability == pytest.approx(sigmoid(-2.0), abs=1e-6)

    def test_speaker_branch_abstains_without_enrolment(self):
        """NOT_ENROLLED is not the same as MISMATCH."""
        scorer = self._scorer()
        assert scorer.update_speaker(None) == SpeakerStatus.NOT_ENROLLED
        assert scorer.speaker_status == SpeakerStatus.NOT_ENROLLED

    def test_speaker_threshold(self):
        scorer = self._scorer(speaker_threshold=0.25)
        assert scorer.update_speaker(0.10) == SpeakerStatus.MISMATCH
        assert scorer.update_speaker(0.60) == SpeakerStatus.MATCH

    def test_speaker_does_not_move_the_spoof_probability(self):
        """Branches are independent - that is the whole point."""
        scorer = self._scorer()
        scorer.update_spoof(1.0)
        before = scorer.spoof_probability
        scorer.update_speaker(-0.9)
        assert scorer.spoof_probability == before

    def test_uncalibrated_scorer_still_ranks(self):
        scorer = Scorer(ScoringConfig(), calibrator=None)
        assert not scorer.calibrated
        low = scorer.update_spoof(-3.0)
        scorer.reset()
        assert low < scorer.update_spoof(3.0)


class TestPolicy:
    def _policy(self):
        return PolicyEngine(PolicyConfig())

    def test_band_maps_to_action(self):
        engine = self._policy()
        assert engine.evaluate(Risk.GREEN, 0.2).action.value == "PROCEED"
        assert engine.evaluate(Risk.AMBER, 0.6).action.value == "CHALLENGE"
        assert engine.evaluate(Risk.RED, 0.9).action.value == "GATE_ACTION"

    def test_decision_carries_a_readable_reason(self):
        decision = self._policy().evaluate(Risk.RED, 0.91)
        assert "0.91" in decision.reason

    def test_identity_mismatch_raises_a_separate_flag(self):
        """A synthetic voice and the wrong person are different findings."""
        decision = self._policy().evaluate(Risk.GREEN, 0.1, SpeakerStatus.MISMATCH)
        assert decision.risk == Risk.GREEN  # band unchanged
        assert decision.identity_warning is True

    def test_not_enrolled_raises_no_warning(self):
        decision = self._policy().evaluate(Risk.GREEN, 0.1, SpeakerStatus.NOT_ENROLLED)
        assert decision.identity_warning is False

    def test_unverified_verdict_fails_closed(self):
        decision = self._policy().evaluate(Risk.GREEN, 0.05, verdict_verified=False)
        assert decision.risk == Risk.RED
        assert "failing closed" in decision.reason

    def test_terminate_call_is_rejected_at_config_load(self):
        with pytest.raises(ValueError, match="never terminates"):
            ActionConfig(red="TERMINATE_CALL")


class TestLiveness:
    def _utterances(self, gaps_ms: list[float], duration_s: float = 2.0):
        utterances, clock, side = [], 0.0, Side.AGENT
        for gap in gaps_ms:
            utterances.append(Utterance(side, clock, clock + duration_s))
            clock = clock + duration_s + gap / 1000.0
            side = Side.CALLER if side == Side.AGENT else Side.AGENT
        return utterances

    def test_machine_floor_is_detected(self):
        config = LivenessConfig()
        utterances, clock = [], 0.0
        for i in range(12):
            side = Side.AGENT if i % 2 == 0 else Side.CALLER
            utterances.append(Utterance(side, clock, clock + 2.0))
            gap = 320.0 if side == Side.AGENT else -80.0
            clock = clock + 2.0 + gap / 1000.0
        features = compute_features(build_transitions(utterances, config), config)
        assert features.floor_delta_ms is not None and features.floor_delta_ms > 200

    def test_overlap_is_recognised(self):
        config = LivenessConfig()
        utterances = self._utterances([-150.0, 200.0, -100.0, 250.0, -120.0, 180.0])
        features = compute_features(build_transitions(utterances, config), config)
        assert (features.caller_overlap_rate or 0) + (features.agent_overlap_rate or 0) > 0

    def test_branch_abstains_below_minimum_transitions(self):
        config = LivenessConfig(min_transitions=6)
        features = compute_features(
            build_transitions(self._utterances([200.0, 180.0]), config), config
        )
        assert features_to_llr(features, config) is None

    def test_short_utterances_are_filtered(self):
        config = LivenessConfig(min_utterance_ms=250)
        utterances = [
            Utterance(Side.AGENT, 0.0, 0.05),
            Utterance(Side.CALLER, 1.0, 3.0),
        ]
        assert build_transitions(utterances, config) == []

    def test_tracker_pairs_start_and_end(self):
        branch = LivenessBranch(LivenessConfig())
        branch.record(TurnEvent(side=Side.CALLER, kind=TurnEventKind.START, monotonic_ns=0))
        branch.record(
            TurnEvent(side=Side.CALLER, kind=TurnEventKind.END, monotonic_ns=2_000_000_000)
        )
        branch.tracker.close(3.0)
        assert branch.tracker.utterances[0].duration_ms == pytest.approx(2000.0)


class TestSession:
    @pytest.mark.asyncio
    async def test_emits_the_documented_contract(self):
        config = load_config("configs")
        messages: list[dict] = []

        async def collect(payload):
            messages.append(payload)

        adapter = SyntheticAdapter(n_turns=12, seed=3, realtime=False)
        session = CallSession(
            session_id="contract",
            config=config,
            detector=StubDetector(config.model.audio.window_samples),
            vad=EnergyVAD(config.model.audio.vad_frame),
            on_message=collect,
        )
        await session.consume(adapter, Side.CALLER)

        assert messages
        last = messages[-1]
        for field in (
            "session_id",
            "sequence",
            "speech_seconds",
            "spoof_probability",
            "risk",
            "speaker_status",
            "inference_ms",
            "model_version",
        ):
            assert field in last
        assert 0.0 <= last["spoof_probability"] <= 1.0
        assert last["risk"] in ("GREEN", "AMBER", "RED")
        session.close()

    @pytest.mark.asyncio
    async def test_liveness_is_off_unless_enabled(self):
        config = load_config("configs")
        session = CallSession(
            session_id="off",
            config=config,
            detector=StubDetector(config.model.audio.window_samples),
            vad=EnergyVAD(config.model.audio.vad_frame),
        )
        assert session.liveness is None
        session.close()

    @pytest.mark.asyncio
    async def test_enabled_liveness_separates_human_from_machine(self):
        config = load_config("configs")
        config.model.liveness.enabled = True

        async def run(floor_ms: float):
            adapter = SyntheticAdapter(
                n_turns=16, pipeline_floor_ms=floor_ms, seed=3, realtime=False
            )
            session = CallSession(
                session_id=f"t{floor_ms}",
                config=config,
                detector=StubDetector(config.model.audio.window_samples),
                vad=EnergyVAD(config.model.audio.vad_frame),
            )
            await asyncio.gather(
                session.consume(adapter, Side.CALLER),
                session.consume(adapter, Side.AGENT),
            )
            payload = session.finalise()
            session.close()
            return payload

        human, machine = await run(0.0), await run(320.0)
        assert machine.liveness.caller_floor_ms > human.liveness.caller_floor_ms

    @pytest.mark.asyncio
    async def test_teardown_clears_the_buffer(self):
        config = load_config("configs")
        adapter = SyntheticAdapter(n_turns=6, seed=1, realtime=False)
        session = CallSession(
            session_id="teardown",
            config=config,
            detector=StubDetector(config.model.audio.window_samples),
            vad=EnergyVAD(config.model.audio.vad_frame),
        )
        await session.consume(adapter, Side.CALLER)
        session.close()
        assert len(session.buffer) == 0

    @pytest.mark.asyncio
    async def test_staging_handles_frames_smaller_than_the_vad_frame(self):
        """Transport frames are 320 samples; the VAD frame is 512."""
        config = load_config("configs")
        adapter = SyntheticAdapter(n_turns=8, seed=2, realtime=False)
        session = CallSession(
            session_id="staging",
            config=config,
            detector=StubDetector(config.model.audio.window_samples),
            vad=EnergyVAD(config.model.audio.vad_frame),
        )
        await session.consume(adapter, Side.CALLER)
        assert session.stats.frames_ingested > 0
        assert session.stats.windows_scored > 0
        session.close()


class TestAudioConfig:
    def test_hop_cannot_exceed_window(self):
        with pytest.raises(ValueError):
            AudioConfig(window_samples=1000, hop_samples=2000)

    def test_unusual_sample_rate_rejected(self):
        with pytest.raises(ValueError):
            AudioConfig(sample_rate=44100)
