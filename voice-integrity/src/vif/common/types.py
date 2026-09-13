"""Shared data types.

These are the contracts between layers.  Everything that crosses a module
boundary is one of these, so a change here surfaces as a type error rather
than as a silently wrong number.

The output contract deliberately exposes a **probability**, not an internal
log-likelihood ratio.  A frontend can render 0.87 without knowing anything
about calibration, and a judge can read it without a lecture.  The scoring
layer keeps whatever internal representation it needs and converts at the
boundary.
"""

from __future__ import annotations

import time
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field


class Side(str, Enum):
    """Which end of the call a stream belongs to.

    CALLER is the party under assessment.  AGENT is our own side: a known
    human, and the in-call reference the liveness branch compares against.
    """

    CALLER = "caller"
    AGENT = "agent"


class Risk(str, Enum):
    """Deterministic band derived from spoof probability."""

    GREEN = "GREEN"
    AMBER = "AMBER"
    RED = "RED"


class SpeakerStatus(str, Enum):
    """Outcome of the optional speaker-verification branch.

    NOT_ENROLLED is distinct from MISMATCH.  A branch that has nothing to
    compare against must say so rather than reporting a low similarity, or the
    absence of an enrolment silently becomes evidence of impersonation.
    """

    NOT_ENROLLED = "NOT_ENROLLED"
    MATCH = "MATCH"
    MISMATCH = "MISMATCH"


class Action(str, Enum):
    PROCEED = "PROCEED"
    CHALLENGE = "CHALLENGE"
    GATE_ACTION = "GATE_ACTION"


class TurnEventKind(str, Enum):
    START = "start"
    END = "end"


class TurnEvent(BaseModel):
    """A speech boundary.  The entire feature set of the liveness branch.

    Deliberately carries no audio, no features and no transcript - which is
    why that branch can run where the acoustic branch legally cannot.
    """

    side: Side
    kind: TurnEventKind
    monotonic_ns: int

    @property
    def seconds(self) -> float:
        return self.monotonic_ns / 1e9


class LivenessFeatures(BaseModel):
    """Shape statistics of the response-gap distribution.

    Every feature is a within-call comparison against the agent side, which is
    what cancels language, network, task and speaker-pair effects.
    """

    n_transitions: int = 0
    caller_floor_ms: float | None = None
    agent_floor_ms: float | None = None
    floor_delta_ms: float | None = None
    caller_overlap_rate: float | None = None
    agent_overlap_rate: float | None = None
    overlap_delta: float | None = None
    caller_gap_cv: float | None = None
    agent_gap_cv: float | None = None
    variance_ratio: float | None = None
    fast_response_count: int = 0
    rtt_ms: float | None = None


class StreamMessage(BaseModel):
    """Streaming result.  Advisory only - no action may be taken on one.

    Branches are reported side by side rather than fused into a single
    number.  With one primary detector and one optional verifier, a combined
    score would add the appearance of rigour without the evidence to support
    it, and it would hide which branch actually fired.
    """

    session_id: str
    sequence: int
    speech_seconds: float
    spoof_probability: float
    risk: Risk
    speaker_similarity: float | None = None
    speaker_status: SpeakerStatus = SpeakerStatus.NOT_ENROLLED
    inference_ms: float = 0.0
    model_version: str = "unknown"
    liveness_score: float | None = None
    type: Literal["score"] = "score"


class VerdictPayload(BaseModel):
    """The signed decision.  The only output an action may be taken on."""

    session_id: str
    spoof_probability: float
    risk: Risk
    speaker_status: SpeakerStatus = SpeakerStatus.NOT_ENROLLED
    speaker_similarity: float | None = None
    action: Action = Action.PROCEED
    speech_seconds: float = 0.0
    windows_scored: int = 0
    liveness: LivenessFeatures | None = None
    model_version: str = "unknown"
    model_checksum: str = ""
    policy_version: str = "policy-1.0.0"
    timestamp: int = Field(default_factory=lambda: int(time.time() * 1000))
    nonce: str = ""


class Verdict(BaseModel):
    payload: VerdictPayload
    algorithm: str = "Ed25519"
    key_id: str = ""
    signature: str = ""


class SessionInfo(BaseModel):
    """Returned by POST /v1/session."""

    session_id: str
    model_version: str
    device: str = "cpu"
    sample_rate: int = 16000
    window_samples: int = 64600
    hop_samples: int = 16000
