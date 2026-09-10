"""Conversational liveness (Branch D).

A different question from the rest of the system.  Branch A asks whether a
waveform was manufactured; this asks whether a *machine sits in the
conversational loop*.  That distinction matters because it partially dissolves
the unseen-synthesizer problem: we are not recognising a generator, we are
detecting that processing is happening at all.

Design decisions worth understanding before changing anything here.

**We measure shape, never the mean.**  "A converter adds 300 ms, so flag slow
responses" dies immediately, because a poor mobile connection also adds
300 ms.  Network latency shifts the whole distribution uniformly; it does not
impose a hard floor, remove overlaps, or compress variance.

**Comparison is within-call.**  The agent side is a known human, so it is the
reference.  This cancels network, task, register, speaker pair - and language.
That last one is not optional: turn-taking gaps vary widely across languages
(near-zero modal gaps in Japanese, several hundred milliseconds in Danish),
and there is no published data for Indian-language telephone registers.  An
absolute threshold would be a false-alarm generator across exactly the
languages the problem statement cares about.

**The entire feature set is timestamps.**  No audio, no embeddings, no
transcript.  Which is why this branch can run where the acoustic branch
legally cannot - a telecom operator barred from touching call content can
still run it.

Honest limitation: this needs 10-20 transitions for strong evidence, so it is
not a fast detector in general.  It is fast in two specific cases worth
demoing - the first transition when the offset is large against measured RTT,
and pre-recorded playback, which has no turn-taking behaviour at all and
collapses on turn one.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from vif.common.config import LivenessConfig
from vif.common.logging import get_logger
from vif.common.types import LivenessFeatures, Side, TurnEvent, TurnEventKind

log = get_logger(__name__)


@dataclass
class Utterance:
    """A contiguous run of speech on one side."""

    side: Side
    start_s: float
    end_s: float

    @property
    def duration_ms(self) -> float:
        return (self.end_s - self.start_s) * 1000.0


@dataclass
class Transition:
    """A speaker change: `from_side` stops, `to_side` starts.

    `gap_ms` is negative when the two overlap.  Negative gaps are the cleanest
    signal in the whole scheme, because network latency cannot manufacture one.
    """

    from_side: Side
    to_side: Side
    gap_ms: float

    @property
    def is_overlap(self) -> bool:
        return self.gap_ms < 0.0


@dataclass
class TurnTracker:
    """Accumulates raw VAD boundaries into utterances.

    Stores nothing but side, kind and a monotonic clock reading.
    """

    events: list[TurnEvent] = field(default_factory=list)
    _open: dict[Side, float] = field(default_factory=dict)
    utterances: list[Utterance] = field(default_factory=list)

    def record(self, event: TurnEvent) -> None:
        self.events.append(event)
        if event.kind == TurnEventKind.START:
            self._open.setdefault(event.side, event.seconds)
        else:
            start = self._open.pop(event.side, None)
            if start is not None:
                self.utterances.append(Utterance(event.side, start, event.seconds))

    def close(self, now_s: float) -> None:
        """Close any utterance still open at teardown."""
        for side, start in list(self._open.items()):
            self.utterances.append(Utterance(side, start, now_s))
        self._open.clear()
        self.utterances.sort(key=lambda u: u.start_s)


def build_transitions(
    utterances: list[Utterance],
    config: LivenessConfig,
) -> list[Transition]:
    """Reduce utterances to speaker-change events.

    We cannot distinguish "turn ended" from "paused mid-sentence" without
    semantics, and we have none.  So we do not try: the definition below is
    operational and must be reported alongside every number it produces.

        A speaker-change event occurs when side A's speech ends, side B's
        speech begins within a window, and A does not resume within T ms.

    Backchannels ("mm-hm") are continuers, not turns, so short utterances are
    excluded from the *initiating* side.
    """
    kept = [u for u in utterances if u.duration_ms >= config.min_utterance_ms]
    kept.sort(key=lambda u: u.start_s)

    transitions: list[Transition] = []
    for i, current in enumerate(kept):
        nxt = next((u for u in kept[i + 1 :] if u.side != current.side), None)
        if nxt is None:
            continue
        # Backchannel filter: a very short reply is a continuer, not a turn.
        if (
            nxt.duration_ms < config.backchannel_max_ms
            and nxt.duration_ms < current.duration_ms / 3
        ):
            continue
        # The initiating side must not resume before the other side starts.
        resumed = any(
            u.side == current.side
            and current.end_s < u.start_s < nxt.start_s
            and (u.start_s - current.end_s) * 1000.0 < config.resume_window_ms
            for u in kept
        )
        if resumed:
            continue
        transitions.append(
            Transition(
                from_side=current.side,
                to_side=nxt.side,
                gap_ms=(nxt.start_s - current.end_s) * 1000.0,
            )
        )
    return transitions


def _floor_ms(gaps: np.ndarray, quantile: float = 0.05) -> float | None:
    """Lower edge of the response-gap distribution.

    A low quantile rather than the minimum, so one mis-detected boundary
    cannot define the floor.
    """
    if gaps.size == 0:
        return None
    return float(np.quantile(gaps, quantile))


def _coefficient_of_variation(gaps: np.ndarray) -> float | None:
    if gaps.size < 2:
        return None
    mean = float(np.mean(gaps))
    if abs(mean) < 1e-6:
        return None
    return float(np.std(gaps) / abs(mean))


def compute_features(
    transitions: list[Transition],
    config: LivenessConfig,
    rtt_ms: float | None = None,
) -> LivenessFeatures:
    """Shape statistics, computed per side and compared within the call."""
    caller_gaps = np.array([t.gap_ms for t in transitions if t.to_side == Side.CALLER])
    agent_gaps = np.array([t.gap_ms for t in transitions if t.to_side == Side.AGENT])

    features = LivenessFeatures(n_transitions=len(transitions), rtt_ms=rtt_ms)

    features.caller_floor_ms = _floor_ms(caller_gaps)
    features.agent_floor_ms = _floor_ms(agent_gaps)
    if features.caller_floor_ms is not None and features.agent_floor_ms is not None:
        # Positive means the caller has a floor the agent does not - the
        # signature of a buffered pipeline.
        features.floor_delta_ms = features.caller_floor_ms - features.agent_floor_ms

    if caller_gaps.size:
        features.caller_overlap_rate = float((caller_gaps < 0).mean())
        features.fast_response_count = int((caller_gaps < config.fast_response_ms).sum())
    if agent_gaps.size:
        features.agent_overlap_rate = float((agent_gaps < 0).mean())
    if features.caller_overlap_rate is not None and features.agent_overlap_rate is not None:
        features.overlap_delta = features.agent_overlap_rate - features.caller_overlap_rate

    features.caller_gap_cv = _coefficient_of_variation(caller_gaps)
    features.agent_gap_cv = _coefficient_of_variation(agent_gaps)
    if features.caller_gap_cv and features.agent_gap_cv:
        features.variance_ratio = features.caller_gap_cv / features.agent_gap_cv

    return features


def features_to_llr(features: LivenessFeatures, config: LivenessConfig) -> float | None:
    """Combine shape features into a log-likelihood ratio.

    Returns None below `min_transitions` rather than a weak score.  A branch
    that has not seen enough evidence should abstain, not guess: an
    unjustified small LLR still moves the fused posterior.

    The weights here are a documented prior, not a fit.  Replace them with
    coefficients learned from the collected corpus (notebook 07) before
    quoting any number from this branch.
    """
    if features.n_transitions < config.min_transitions:
        return None

    llr = 0.0

    # 1. Floor differential.  A pipeline cannot emit faster than its buffer.
    if features.floor_delta_ms is not None:
        llr += float(np.clip(features.floor_delta_ms / 150.0, -2.0, 2.0))

    # 2. Overlap.  Half-duplex conversion cannot produce correct overlap, and
    #    latency cannot manufacture a negative gap.  Strongest single feature.
    if features.overlap_delta is not None:
        llr += float(np.clip(features.overlap_delta * 6.0, -2.0, 2.0))

    # 3. Absence of fast responses despite ample opportunity.
    #    P(no fast response | human) = (1 - p)^n.  With p about 0.15, ten
    #    transitions gives roughly 0.20 - suggestive, not conclusive.  The
    #    log-odds below encode exactly that strength and no more.
    if features.fast_response_count == 0 and features.n_transitions >= config.min_transitions:
        p_fast = 0.15
        evidence = -math.log(max((1.0 - p_fast) ** features.n_transitions, 1e-6))
        llr += float(np.clip(evidence, 0.0, 1.5))

    # 4. Compressed variance relative to the known human in the same call.
    if features.variance_ratio is not None:
        llr += float(np.clip((1.0 - features.variance_ratio) * 1.2, -1.0, 1.0))

    return float(np.clip(llr, -4.0, 4.0))


class LivenessBranch:
    """Per-call liveness state.

    Runs once at teardown over the collected timestamps, not per window, which
    is why it costs nothing during the call.  `snapshot()` exists so the live
    dashboard can show a provisional value mid-call.
    """

    def __init__(self, config: LivenessConfig | None = None, rtt_ms: float | None = None):
        self.config = config or LivenessConfig()
        self.tracker = TurnTracker()
        self.rtt_ms = rtt_ms
        self._features: LivenessFeatures | None = None

    def record(self, event: TurnEvent) -> None:
        self.tracker.record(event)

    def set_rtt(self, rtt_ms: float | None) -> None:
        """Transport RTT from RTCP.

        Taken from the transport layer, never estimated acoustically: an echo
        probe would be fighting a well-tuned adaptive echo canceller for a
        number the stack already provides (FR-IN-06).
        """
        self.rtt_ms = rtt_ms

    def snapshot(self, now_s: float) -> tuple[LivenessFeatures, float | None]:
        """Provisional features without closing the call."""
        tracker = TurnTracker(
            events=list(self.tracker.events),
            _open=dict(self.tracker._open),
            utterances=list(self.tracker.utterances),
        )
        tracker.close(now_s)
        transitions = build_transitions(tracker.utterances, self.config)
        features = compute_features(transitions, self.config, self.rtt_ms)
        return features, features_to_llr(features, self.config)

    def finalise(self, now_s: float) -> tuple[LivenessFeatures, float | None]:
        self.tracker.close(now_s)
        transitions = build_transitions(self.tracker.utterances, self.config)
        self._features = compute_features(transitions, self.config, self.rtt_ms)
        llr = features_to_llr(self._features, self.config)
        log.info(
            "liveness: %d transitions, floor_delta=%s ms, overlap_delta=%s, llr=%s",
            self._features.n_transitions,
            _fmt(self._features.floor_delta_ms),
            _fmt(self._features.overlap_delta),
            _fmt(llr),
        )
        return self._features, llr

    @property
    def features(self) -> LivenessFeatures:
        return self._features or LivenessFeatures()


def _fmt(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.3f}"
