"""Serving layer: ring buffer, VAD, fusion, policy, liveness, session."""

from __future__ import annotations

import asyncio

import numpy as np
import pytest

from vif.common.config import (
    AudioConfig,
    FusionConfig,
    LivenessConfig,
    PolicyConfig,
    TierConfig,
    load_config,
)
from vif.common.types import Band, BranchScores, Side, TurnEvent, TurnEventKind
from vif.eval.calibration import Calibrator, PlattParams
from vif.serve.adapters.file import SyntheticAdapter
from vif.serve.detector import StubDetector
from vif.serve.fusion import FusionEngine, metadata_prior_llr
from vif.serve.liveness import (
    LivenessBranch,
    Utterance,
    build_transitions,
    compute_features,
    features_to_llr,
)
from vif.serve.policy import PolicyEngine
from vif.serve.ringbuffer import SpeechRingBuffer
from vif.serve.session import CallSession
from vif.serve.vad import EnergyVAD


class TestRingBuffer:
    def test_window_needs_a_full_hop(self):
        buf = SpeechRingBuffer(window_samples=1000, hop_samples=100)
        buf.append(np.ones(1000, dtype=np.float32))
        assert buf.take_window() is not None
        # A window was just taken; another needs a whole hop of new speech.
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


class TestFusion:
    def _engine(self, **kw):
        return FusionEngine(
            FusionConfig(weights={"spoof": 1.0, "speaker": 0.0, "prosody": 0.0, "liveness": 1.0}),
            Calibrator({"spoof": PlattParams(a=1.0, b=0.0, fitted_on="dev")}),
            **kw,
        )

    def test_evidence_accumulates_rather_than_averages(self):
        """Ten weak windows must be stronger than one, not equal to it."""
        engine = self._engine()
        for _ in range(10):
            engine.update(BranchScores(spoof=0.5))
        assert engine.state.running_llr == pytest.approx(5.0)

        single = self._engine()
        single.update(BranchScores(spoof=0.5))
        assert engine.risk > single.risk

    def test_confidence_interval_narrows(self):
        engine = self._engine()
        engine.update(BranchScores(spoof=0.5))
        first = engine.confidence_interval
        for _ in range(20):
            engine.update(BranchScores(spoof=0.5))
        assert engine.confidence_interval < first

    def test_abstaining_branch_does_not_move_the_posterior(self):
        engine = self._engine()
        before = engine.state.running_llr
        engine.update(BranchScores(spoof=None, speaker=None, prosody=None))
        assert engine.state.running_llr == before
        assert engine.state.n_windows == 0

    def test_per_window_contribution_is_clamped(self):
        engine = self._engine()
        engine.update(BranchScores(spoof=1000.0))
        assert abs(engine.state.running_llr) <= 4.0

    def test_prior_shifts_the_starting_point(self):
        neutral = self._engine(prior_llr=0.0)
        suspicious = self._engine(prior_llr=2.0)
        assert suspicious.risk > neutral.risk

    def test_metadata_prior_direction(self):
        policy = PolicyConfig(
            metadata_prior={"first_contact": 0.8, "known_contact": -1.2},
        )
        assert metadata_prior_llr({"first_contact": True}, policy) > 0
        assert metadata_prior_llr({"known_contact": True}, policy) < 0
        assert metadata_prior_llr({}, policy) == 0.0


class TestPolicy:
    def _policy(self):
        return PolicyEngine(
            PolicyConfig(
                tiers=[
                    TierConfig(name="routine", max_value=50000, amber=55, red=85),
                    TierConfig(name="high", max_value=None, amber=35, red=70),
                ]
            )
        )

    def test_tier_selection_by_value(self):
        engine = self._policy()
        assert engine.select_tier(1000).name == "routine"
        assert engine.select_tier(10_000_000).name == "high"

    def test_unknown_value_uses_the_strictest_tier(self):
        """An unknown stake is not the same as a low one."""
        assert self._policy().select_tier(None).name == "high"

    def test_same_risk_different_tiers(self):
        engine = self._policy()
        assert engine.evaluate(60.0, transaction_value=1000).band == Band.AMBER
        assert engine.evaluate(60.0, transaction_value=10_000_000).band == Band.AMBER
        assert engine.evaluate(75.0, transaction_value=1000).band == Band.AMBER
        assert engine.evaluate(75.0, transaction_value=10_000_000).band == Band.RED

    def test_unverified_verdict_fails_closed(self):
        decision = self._policy().evaluate(0.0, 1000, verdict_verified=False)
        assert decision.band == Band.RED
        assert "failing closed" in decision.reason

    def test_terminate_call_is_rejected_at_config_load(self):
        with pytest.raises(ValueError, match="never terminate"):
            PolicyConfig(
                actions={"green": "proceed", "amber": "challenge", "red": "terminate_call"}
            )

    def test_amber_must_be_below_red(self):
        with pytest.raises(ValueError, match="below red"):
            PolicyConfig(tiers=[TierConfig(name="bad", max_value=None, amber=90, red=50)])


class TestLiveness:
    def _utterances(self, gaps_ms: list[float], duration_s: float = 2.0):
        """Build an alternating conversation with the given response gaps."""
        utterances, clock, side = [], 0.0, Side.AGENT
        for gap in gaps_ms:
            utterances.append(Utterance(side, clock, clock + duration_s))
            clock = clock + duration_s + gap / 1000.0
            side = Side.CALLER if side == Side.AGENT else Side.AGENT
        return utterances

    def test_machine_floor_is_detected(self):
        config = LivenessConfig()
        # Agent responds naturally, caller never faster than 300 ms.
        utterances, clock = [], 0.0
        for i in range(12):
            side = Side.AGENT if i % 2 == 0 else Side.CALLER
            utterances.append(Utterance(side, clock, clock + 2.0))
            gap = 320.0 if side == Side.AGENT else -80.0  # next is caller / agent
            clock = clock + 2.0 + gap / 1000.0
        features = compute_features(build_transitions(utterances, config), config)
        assert features.floor_delta_ms is not None
        assert features.floor_delta_ms > 200

    def test_overlap_is_recognised(self):
        config = LivenessConfig()
        utterances = self._utterances([-150.0, 200.0, -100.0, 250.0, -120.0, 180.0])
        features = compute_features(build_transitions(utterances, config), config)
        assert (features.caller_overlap_rate or 0) + (features.agent_overlap_rate or 0) > 0

    def test_branch_abstains_below_minimum_transitions(self):
        """Too little evidence must abstain, not guess."""
        config = LivenessConfig(min_transitions=6)
        features = compute_features(
            build_transitions(self._utterances([200.0, 180.0]), config), config
        )
        assert features_to_llr(features, config) is None

    def test_short_utterances_are_filtered(self):
        config = LivenessConfig(min_utterance_ms=250)
        utterances = [
            Utterance(Side.AGENT, 0.0, 0.05),  # 50 ms blip
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
        assert len(branch.tracker.utterances) == 1
        assert branch.tracker.utterances[0].duration_ms == pytest.approx(2000.0)

    def test_open_utterance_closed_at_teardown(self):
        branch = LivenessBranch(LivenessConfig())
        branch.record(TurnEvent(side=Side.AGENT, kind=TurnEventKind.START, monotonic_ns=0))
        branch.finalise(5.0)
        assert len(branch.tracker.utterances) == 1


class TestSession:
    @pytest.mark.asyncio
    async def test_machine_call_scores_higher_than_human(self):
        """The end-to-end claim, on synthetic conversations."""
        config = load_config("configs")

        async def run(floor_ms: float):
            adapter = SyntheticAdapter(
                n_turns=16, pipeline_floor_ms=floor_ms, seed=3, realtime=False
            )
            session = CallSession(
                call_id=f"t{floor_ms}",
                config=config,
                detector=StubDetector(config.model.audio.window_samples),
                vad=EnergyVAD(config.model.audio.vad_frame),
                calibrator=Calibrator({}),
            )
            await asyncio.gather(
                session.consume(adapter, Side.CALLER),
                session.consume(adapter, Side.AGENT),
            )
            payload = session.finalise()
            session.close()
            return payload

        human = await run(0.0)
        machine = await run(320.0)

        assert machine.liveness.n_transitions > 0
        assert machine.liveness.caller_floor_ms > human.liveness.caller_floor_ms
        assert (machine.branches.liveness or 0) > (human.branches.liveness or 0)

    @pytest.mark.asyncio
    async def test_teardown_clears_the_buffer(self):
        config = load_config("configs")
        adapter = SyntheticAdapter(n_turns=6, seed=1, realtime=False)
        session = CallSession(
            call_id="teardown",
            config=config,
            detector=StubDetector(config.model.audio.window_samples),
            vad=EnergyVAD(config.model.audio.vad_frame),
            calibrator=Calibrator({}),
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
            call_id="staging",
            config=config,
            detector=StubDetector(config.model.audio.window_samples),
            vad=EnergyVAD(config.model.audio.vad_frame),
            calibrator=Calibrator({}),
        )
        await asyncio.gather(
            session.consume(adapter, Side.CALLER),
            session.consume(adapter, Side.AGENT),
        )
        assert session.stats.frames_ingested > 0
        assert len(session.liveness.tracker.events) > 0
        session.close()


class TestAudioConfig:
    def test_hop_cannot_exceed_window(self):
        with pytest.raises(ValueError):
            AudioConfig(window_samples=1000, hop_samples=2000)

    def test_unusual_sample_rate_rejected(self):
        with pytest.raises(ValueError):
            AudioConfig(sample_rate=44100)
